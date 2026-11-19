from datetime import UTC, datetime
from typing import Annotated

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    Security,
)
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audible.types import (
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.series.audible import SeriesInfo, get_series
from app.internal.series.config import (
    RequestedBook,
    SeriesMode,
    WatchedSeries,
    series_config,
)
from app.internal.series.job import (
    ensure_series_user,
    is_running,
    plan_for,
    request_books,
    run_series_check,
    start_downloads,
)
from app.internal.series.plan import SeriesPlan
from app.util.connection import get_connection
from app.util.db import get_session

router = APIRouter(prefix="/series", tags=["Series"])


class SeriesView(BaseModel):
    info: SeriesInfo
    plan: SeriesPlan
    """What the watch list would ask for right now, in the mode shown."""
    mode: SeriesMode
    watched: WatchedSeries | None


class WatchBody(BaseModel):
    mode: SeriesMode | None = None
    """Defaults to the configured default mode."""
    region: audible_region_type | None = None
    title: str | None = None
    """Helps to find the series if Audible does not list it directly."""


class ModeBody(BaseModel):
    mode: SeriesMode


class WatchStatus(BaseModel):
    running: bool
    enabled: bool
    last_run: datetime | None
    series: list[WatchedSeries]


async def load_series(
    session: Session,
    client_session: ClientSession,
    asin: str,
    region: audible_region_type | None = None,
    title: str | None = None,
    mode: SeriesMode | None = None,
    force: bool = False,
) -> SeriesView:
    watched = series_config.get_series(session, asin)
    if region is None:
        if watched and watched.region in audible_regions:
            region = watched.region
        else:
            region = get_region_from_settings()
    if audible_regions.get(region) is None:
        raise HTTPException(status_code=400, detail="Invalid region")
    info = await get_series(
        client_session,
        asin,
        region,
        title_hint=title or (watched.title if watched else None),
        force=force,
    )
    if info is None:
        raise HTTPException(
            status_code=404, detail="The series could not be found on Audible"
        )
    shown_mode: SeriesMode = (
        mode
        or (watched.mode if watched else None)
        or series_config.get_default_mode(session)
    )
    plan = plan_for(
        session,
        info,
        shown_mode,
        watched.added_at if watched else datetime.now(UTC),
    )
    return SeriesView(info=info, plan=plan, mode=shown_mode, watched=watched)


@router.get("/watch", response_model=WatchStatus)
def list_watched(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth())],
):
    watch = series_config.get_watch(session)
    return WatchStatus(
        running=is_running(),
        enabled=series_config.get_enabled(session),
        last_run=series_config.get_last_run(session),
        series=sorted(watch.values(), key=lambda s: s.title.lower()),
    )


@router.post("/run", status_code=202)
def start_series_check(
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Checks every watched series now instead of waiting for the daily run."""
    if is_running():
        raise HTTPException(status_code=409, detail="A check is already running")
    background_task.add_task(run_series_check)
    return Response(status_code=202)


@router.get("/{asin}", response_model=SeriesView)
async def get_series_view(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(AnyAuth())],
    region: Annotated[
        audible_region_type | None,
        Query(description="Audible region. Defaults to the configured region."),
    ] = None,
    title: Annotated[
        str | None, Query(description="Series title, as a hint for finding it.")
    ] = None,
    mode: Annotated[
        SeriesMode | None, Query(description="Preview the plan in this mode.")
    ] = None,
):
    """The books of a series in reading order, and what watching it would ask for."""
    return await load_series(session, client_session, asin, region, title, mode)


@router.post("/{asin}/watch", response_model=SeriesView)
async def watch_series(
    asin: str,
    body: WatchBody,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.trusted))],
):
    """Puts a series on the watch list. Nothing is requested until the preview
    in the answer has been confirmed."""
    mode = body.mode or series_config.get_default_mode(session)
    view = await load_series(
        session, client_session, asin, body.region, body.title, mode, force=True
    )
    watched = series_config.get_series(session, asin)
    if watched is None:
        watched = WatchedSeries(
            asin=asin,
            title=view.info.title,
            region=view.info.region,
            mode=mode,
            added_by=user.username,
            added_at=datetime.now(UTC),
            cover_image=view.info.cover_image,
        )
    elif watched.mode != mode:
        # a different mode asks for different books: preview again
        watched.mode = mode
        watched.confirmed = False
    watched.upcoming = view.plan.upcoming_text
    series_config.put_series(session, watched)
    view.watched = watched
    return view


@router.patch("/{asin}/watch", response_model=SeriesView)
async def change_series_mode(
    asin: str,
    body: ModeBody,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.trusted))],
):
    if series_config.get_series(session, asin) is None:
        raise HTTPException(status_code=404, detail="Series is not watched")
    return await watch_series(
        asin, WatchBody(mode=body.mode), session, client_session, user
    )


@router.post("/{asin}/confirm", response_model=SeriesView)
async def confirm_series(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.trusted))],
):
    """Agrees to the preview: requests the missing books now and lets the daily
    check take over from here."""
    watched = series_config.get_series(session, asin)
    if watched is None:
        raise HTTPException(status_code=404, detail="Series is not watched")
    view = await load_series(session, client_session, asin, force=True)
    now = datetime.now(UTC)
    added = request_books(session, view.plan.request, ensure_series_user(session))
    watched.confirmed = True
    watched.last_checked = now
    watched.upcoming = view.plan.upcoming_text
    watched.last_result = (
        f"requested {len(added)} book{'s' if len(added) != 1 else ''}"
        if added
        else "nothing new"
    )
    watched.requested.extend(
        RequestedBook(asin=b.asin, title=b.title, sequence=b.sequence, when=now)
        for b in added
    )
    series_config.put_series(session, watched)
    background_task.add_task(start_downloads, [b.asin for b in added])
    return await load_series(session, client_session, asin)


@router.delete("/{asin}/watch", status_code=204)
def unwatch_series(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.trusted))],
):
    """Takes a series off the watch list. Requests already made are kept."""
    if not series_config.remove_series(session, asin):
        raise HTTPException(status_code=404, detail="Series is not watched")
    return Response(status_code=204)
