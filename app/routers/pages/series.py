import json
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Security
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audible.search import upsert_books
from app.internal.audible.types import audible_region_type
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import AudiobookWithRequests, GroupEnum
from app.internal.ranking.quality import quality_config
from app.internal.series.audible import SeriesBook
from app.internal.series.config import SeriesMode, series_config
from app.internal.series.job import is_running
from app.routers.api.series import (
    SeriesView,
    WatchBody,
    confirm_series,
    load_series,
    start_series_check,
    unwatch_series,
    watch_series,
)
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response
from app.util.toast import ToastException

router = APIRouter(prefix="/series")


class SeriesRow(BaseModel):
    book: SeriesBook
    card: AudiobookWithRequests
    status: str
    """have, request, upcoming or skipped"""
    note: str


def _rows(session: Session, view: SeriesView, user: DetailedUser) -> list[SeriesRow]:
    stored = {
        b.asin: b
        for b in upsert_books(session, [b.to_audiobook() for b in view.info.books])
    }
    notes: dict[str, tuple[str, str]] = {}
    for item in view.plan.have:
        notes[item.book.asin] = ("have", item.reason)
    for book in view.plan.request:
        notes[book.asin] = ("request", "missing")
    for book in view.plan.upcoming:
        notes[book.asin] = ("upcoming", f"out {book.release_text}")
    for item in view.plan.skipped:
        notes[item.book.asin] = ("skipped", item.reason)

    rows: list[SeriesRow] = []
    for book in view.info.books:
        cached = stored.get(book.asin)
        if cached is None:
            continue
        status, note = notes.get(book.asin, ("skipped", ""))
        rows.append(
            SeriesRow(
                book=book,
                card=AudiobookWithRequests(
                    book=cached, requests=cached.requests, username=user.username
                ),
                status=status,
                note=note,
            )
        )
    return rows


def _render_body(
    name: str,
    session: Session,
    view: SeriesView,
    user: DetailedUser,
    toast: str | None = None,
) -> HTMLResponse:
    headers: dict[str, str] = {}
    if toast:
        headers["HX-Trigger"] = json.dumps(
            {"toast": {"type": "success", "message": toast}}
        )
    return catalog_response(
        name,
        headers=headers,
        user=user,
        view=view,
        rows=_rows(session, view, user),
        default_mode=series_config.get_default_mode(session),
        enabled=series_config.get_enabled(session),
        auto_start_download=quality_config.get_auto_download(session)
        and user.is_above(GroupEnum.trusted),
    )


def _render_list(
    name: str,
    session: Session,
    user: DetailedUser,
    toast: str | None = None,
    running: bool | None = None,
) -> HTMLResponse:
    headers: dict[str, str] = {}
    if toast:
        headers["HX-Trigger"] = json.dumps(
            {"toast": {"type": "success", "message": toast}}
        )
    watch = series_config.get_watch(session)
    last_run = series_config.get_last_run(session)
    return catalog_response(
        name,
        headers=headers,
        user=user,
        series=sorted(watch.values(), key=lambda s: s.title.lower()),
        running=is_running() if running is None else running,
        enabled=series_config.get_enabled(session),
        run_hour=series_config.get_run_hour(session),
        last_run=(
            last_run.astimezone().strftime("%b %d, %I:%M %p") if last_run else None
        ),
    )


@router.get("")
def read_watch_list(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    return _render_list("Series.Index", session, user)


@router.post("/hx-run")
def run_now(
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    try:
        _ = start_series_check(background_task, admin_user)
    except HTTPException as e:
        raise ToastException(str(e.detail)) from e
    return _render_list(
        "Series.List",
        session,
        admin_user,
        "Checking every watched series now.",
        running=True,
    )


@router.get("/{asin}")
async def read_series(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    region: audible_region_type | None = None,
    title: str | None = None,
):
    try:
        view = await load_series(session, client_session, asin, region, title)
    except HTTPException as e:
        return catalog_response(
            "Series.Missing", user=user, title=title or "Series", detail=str(e.detail)
        )
    return _render_body("Series.View", session, view, user)


def _respond(
    session: Session, view: SeriesView, user: DetailedUser, toast: str
) -> HTMLResponse:
    return _render_body("Series.Body", session, view, user, toast)


@router.post("/{asin}/hx-watch")
async def watch(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
    mode: Annotated[SeriesMode, Form()],
    region: Annotated[audible_region_type | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
):
    try:
        view = await watch_series(
            asin,
            WatchBody(mode=mode, region=region, title=title),
            session,
            client_session,
            user,
        )
    except HTTPException as e:
        raise ToastException(str(e.detail)) from e
    if view.watched and view.watched.confirmed:
        toast = "Watching this series."
    else:
        toast = "Check the preview, then confirm."
    return _respond(session, view, user, toast)


@router.post("/{asin}/hx-confirm")
async def confirm(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    try:
        view = await confirm_series(
            asin, session, client_session, background_task, user
        )
    except HTTPException as e:
        raise ToastException(str(e.detail)) from e
    result = view.watched.last_result if view.watched else None
    return _respond(
        session, view, user, f"Watching this series: {result or 'nothing new'}."
    )


@router.delete("/{asin}/hx-watch")
async def unwatch(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
    region: audible_region_type | None = None,
    title: str | None = None,
):
    try:
        _ = unwatch_series(asin, session, user)
        view = await load_series(session, client_session, asin, region, title)
    except HTTPException as e:
        raise ToastException(str(e.detail)) from e
    return _respond(session, view, user, "No longer watching this series.")
