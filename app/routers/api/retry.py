from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Response,
    Security,
)
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.matching.config import match_config
from app.internal.models import Audiobook, GroupEnum
from app.internal.query import background_start_query
from app.internal.retry.job import is_running, run_retry
from app.internal.retry.pool import RetryOverview, StageRow, get_overview, get_rows
from app.internal.retry.state import retry_config
from app.util.db import get_session

router = APIRouter(prefix="/retry", tags=["Retry"])


class RetryStatus(RetryOverview):
    enabled: bool
    """Open requests are searched again on a schedule."""
    running: bool
    """A retry run is in progress right now."""
    strict: bool
    """Strict matching is on. Retries do not run without it."""


def read_retry_status(session: Session) -> RetryStatus:
    overview = get_overview(session)
    return RetryStatus(
        pending=overview.pending,
        hunting=overview.hunting,
        grabbed=overview.grabbed,
        enabled=retry_config.get_enabled(session),
        running=is_running(),
        strict=match_config.get_strict(session),
    )


@router.get(
    "",
    response_model=RetryStatus,
    description=(
        "Open requests split into those waiting for an approval and those "
        + "searched automatically, plus the latest automatic downloads."
    ),
)
def get_retry_status(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_retry_status(session)


@router.post(
    "/approve-all",
    status_code=204,
    description="Approve every request that is waiting, then search for them.",
)
def approve_all_requests(
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    pending = [r.book.asin for r in get_overview(session).pending]
    if pending:
        retry_config.approve(session, *pending)
        # never searched before, so a normal run picks all of them up
        background_task.add_task(run_retry)
    return Response(status_code=204)


@router.post(
    "/approve/{asin}",
    status_code=204,
    description="Approve a request for automatic searching and search for it now.",
)
def approve_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    if not session.get(Audiobook, asin):
        raise HTTPException(status_code=404, detail="Book not found")
    retry_config.approve(session, asin)
    background_task.add_task(
        background_start_query, asin_or_uuid=asin, auto_download=True
    )
    return Response(status_code=204)


@router.delete(
    "/approve/{asin}",
    status_code=204,
    description="Take an approval back. The request waits for an admin again.",
)
def unapprove_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    retry_config.unapprove(session, asin)
    return Response(status_code=204)


@router.post(
    "/run",
    status_code=202,
    description=(
        "Search for every request in the pool now, ignoring the wait "
        + "between tries. Runs in the background."
    ),
)
def start_retry_run(
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    if not is_running():
        background_task.add_task(run_retry, True)
    return Response(status_code=202)


class DeclineBody(BaseModel):
    note: str = Field(default="", max_length=2000)


def _book_or_404(session: Session, asin: str) -> Audiobook:
    book = session.get(Audiobook, asin)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def _require_book(session: Session, asin: str) -> None:
    _ = _book_or_404(session, asin)


@router.get(
    "/stages",
    response_model=list[StageRow],
    description="Every requested book with the stage it is in.",
)
def list_stages(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
):
    return get_rows(session, None if user.is_admin() else user.username)


@router.post(
    "/decline/{asin}",
    status_code=204,
    description="Decline a request. The note is shown to the people who asked.",
)
def decline_request(
    asin: str,
    body: DeclineBody,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    book = _book_or_404(session, asin)
    book.admin_note = body.note.strip() or None
    session.add(book)
    session.commit()
    retry_config.unapprove(session, asin)
    retry_config.set_declined(session, asin, True)
    return Response(status_code=204)


@router.delete(
    "/decline/{asin}",
    status_code=204,
    description="Take a decline back. The request returns to where it was.",
)
def restore_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _require_book(session, asin)
    retry_config.set_declined(session, asin, False)
    return Response(status_code=204)


@router.post(
    "/library/{asin}",
    status_code=204,
    description="Mark a book as being in the library, when the check misses it.",
)
def mark_in_library(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    book = _book_or_404(session, asin)
    book.fulfilled = True
    session.add(book)
    session.commit()
    retry_config.mark_in_library(session, asin)
    return Response(status_code=204)


@router.post(
    "/requeue/{asin}",
    status_code=204,
    description="Send a grabbed book back to searching, when its download never arrived.",
)
def requeue_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    book = _book_or_404(session, asin)
    book.fulfilled = False
    session.add(book)
    session.commit()
    retry_config.clear_grab(session, asin)
    # an admin asked for another search, which is an approval
    retry_config.approve(session, asin)
    return Response(status_code=204)


@router.post(
    "/search/{asin}",
    status_code=202,
    description="Search for one book now. Runs in the background.",
)
def search_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _require_book(session, asin)
    background_task.add_task(
        background_start_query, asin_or_uuid=asin, auto_download=True
    )
    return Response(status_code=202)
