import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Form, Security
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.db_queries import get_wishlist_counts
from app.internal.download_backend import get_download_backend
from app.internal.library.owned import (
    library_check_available,
    scheduled_library_check,
)
from app.internal.matching.config import match_config
from app.internal.models import GroupEnum
from app.internal.retry.job import is_running, scheduled_retry
from app.internal.retry.pool import STAGES, StageRow, get_rows
from app.internal.retry.state import retry_config
from app.routers.api.requests import delete_request as api_delete_request
from app.routers.api.retry import (
    DeclineBody,
    approve_all_requests,
    approve_request,
    decline_request,
    mark_in_library,
    requeue_request,
    restore_request,
    search_request,
    start_retry_run,
    unapprove_request,
)
from app.util.db import get_session
from app.util.templates import catalog_response

# How often the scheduler looks for work.
RETRY_CHECK_MINUTES = 10
LIBRARY_CHECK_MINUTES = 10
# A book that reached the library counts as "ready" in the top bar for this long.
READY_DAYS = 7

StageView = Literal["all", "pending", "searching", "downloading", "library", "declined"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_retry, "interval", minutes=RETRY_CHECK_MINUTES)
    scheduler.add_job(
        scheduled_library_check,
        "interval",
        minutes=LIBRARY_CHECK_MINUTES,
        # the first look at the library should not wait for a whole interval
        next_run_time=datetime.now() + timedelta(seconds=20),
    )
    scheduler.start()
    yield
    scheduler.shutdown()


router = APIRouter(prefix="/stages", lifespan=lifespan)


def _view(stage: str | None, user: DetailedUser, rows: list[StageRow]) -> StageView:
    if stage == "all":
        return "all"
    for known in STAGES:
        if stage == known:
            return known
    if not user.is_admin():
        return "all"
    # admins land on what needs them, or on what is being searched for
    if any(r.stage == "pending" for r in rows):
        return "pending"
    return "searching"


def _render(
    name: str,
    session: Session,
    user: DetailedUser,
    stage: str | None,
    toast: str | None = None,
    update_tablist: bool = False,
) -> HTMLResponse:
    rows = get_rows(session, None if user.is_admin() else user.username)
    view = _view(stage, user, rows)
    shown: list[StageRow] = [r for r in rows if view == "all" or r.stage == view]
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
        rows=shown,
        submitters=sorted({name for r in shown for name in r.requesters}),
        counts=get_wishlist_counts(session, user),
        running=is_running(),
        strict=match_config.get_strict(session),
        retry_enabled=retry_config.get_enabled(session),
        can_check=library_check_available(session),
        show_sources=get_download_backend(session) == "prowlarr",
        update_tablist=update_tablist,
    )


def render_stage_page(
    session: Session, user: DetailedUser, stage: str | None
) -> HTMLResponse:
    """The wishlist page: one tab per stage a request can be in."""
    return _render("Wishlist.Stages", session, user, stage)


def _content(
    session: Session,
    user: DetailedUser,
    stage: str | None,
    toast: str | None = None,
) -> HTMLResponse:
    return _render(
        "Wishlist.StageContent", session, user, stage, toast, update_tablist=True
    )


@router.get("/hx-content")
async def stage_content(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    stage: str | None = None,
):
    return _content(session, user, stage)


@router.get("/hx-badge")
async def approval_badge(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    return catalog_response(
        "Wishlist.ApprovalBadge",
        count=get_wishlist_counts(session, admin_user).pending,
    )


@router.get("/hx-status")
async def nav_status(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    compact: bool = False,
):
    """What the top bar shows next to the requests link."""
    rows = get_rows(session, None if user.is_admin() else user.username)
    since = datetime.now(UTC) - timedelta(days=READY_DAYS)
    return catalog_response(
        "Wishlist.NavStatus",
        ready=sum(
            1
            for r in rows
            if r.stage == "library"
            and r.record is not None
            and r.record.in_library_at is not None
            and r.record.in_library_at > since
        ),
        on_the_way=sum(1 for r in rows if r.stage == "downloading"),
        in_progress=sum(
            1 for r in rows if r.stage in ("pending", "searching", "downloading")
        ),
        compact=compact,
    )


@router.post("/hx-approve-all")
async def approve_all(
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = approve_all_requests(session, background_task, admin_user)
    return _content(session, admin_user, stage, "Approved. Searching now.")


@router.post("/hx-approve/{asin}")
async def approve(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = approve_request(asin, session, background_task, admin_user)
    return _content(session, admin_user, stage, "Approved. Searching now.")


@router.delete("/hx-approve/{asin}")
async def unapprove(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = unapprove_request(asin, session, admin_user)
    return _content(session, admin_user, stage, "Approval taken back.")


@router.post("/hx-decline/{asin}")
async def decline(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    note: Annotated[str, Form()] = "",
    stage: str | None = None,
):
    _ = decline_request(asin, DeclineBody(note=note), session, admin_user)
    return _content(session, admin_user, stage, "Request declined.")


@router.delete("/hx-decline/{asin}")
async def restore(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = restore_request(asin, session, admin_user)
    return _content(session, admin_user, stage, "Request restored.")


@router.post("/hx-search/{asin}")
async def search_now(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = search_request(asin, session, background_task, admin_user)
    return _content(session, admin_user, stage, "Searching. Refresh in a moment.")


@router.post("/hx-run")
async def run_now(
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = start_retry_run(background_task, admin_user)
    return _content(session, admin_user, stage, "Search started for every book.")


@router.post("/hx-library/{asin}")
async def in_library(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = mark_in_library(asin, session, admin_user)
    return _content(session, admin_user, stage, "Marked as in the library.")


@router.post("/hx-requeue/{asin}")
async def requeue(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    stage: str | None = None,
):
    _ = requeue_request(asin, session, admin_user)
    return _content(session, admin_user, stage, "Sent back to searching.")


@router.delete("/hx-request/{asin}")
async def remove_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    stage: str | None = None,
):
    _ = await api_delete_request(asin, session, user)
    return _content(session, user, stage, "Request removed.")
