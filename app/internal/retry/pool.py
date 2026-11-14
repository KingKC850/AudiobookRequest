"""The stage every requested book is in.

pending      asked for by untrusted users only: waits for an admin
searching    searched automatically until a strict match is found
downloading  grabbed, not in the library yet
library      in the library (or fulfilled with no way to check)
declined     an admin said no; the reason is the book's admin note
"""

from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel
from sqlalchemy.orm import InstrumentedAttribute, selectinload
from sqlmodel import Session, col, select

from app.internal.audiobookshelf.config import abs_config
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    AudiobookWishlistResult,
    GroupEnum,
    User,
)
from app.internal.retry.state import RetryRecord, retry_config

Stage = Literal["pending", "searching", "downloading", "library", "declined"]
STAGES: tuple[Stage, ...] = (
    "pending",
    "searching",
    "downloading",
    "library",
    "declined",
)


class StageRow(BaseModel):
    result: AudiobookWishlistResult
    stage: Stage
    record: RetryRecord | None
    approved_only: bool = False
    """Searched only because an admin approved it, not because of who asked."""
    stuck: bool = False
    """Grabbed a while ago and still not in the library."""
    requested_at: datetime
    """When the first request for the book was made."""

    @property
    def requesters(self) -> list[str]:
        return sorted(r.user_username for r in self.result.requests)

    @property
    def requested_text(self) -> str:
        return self.requested_at.astimezone().strftime("%b %d, %Y")

    @property
    def stage_date(self) -> datetime:
        """The date the default sort of the row's stage goes by."""
        if self.record and self.record.in_library_at:
            return self.record.in_library_at
        if self.record and self.record.grabbed:
            return self.record.grabbed.when
        return self.requested_at


class HuntingItem(BaseModel):
    result: AudiobookWishlistResult
    approved_only: bool
    record: RetryRecord | None


class GrabbedItem(BaseModel):
    key: str
    record: RetryRecord


class RetryOverview(BaseModel):
    pending: list[AudiobookWishlistResult]
    """Open requests from untrusted users only. Never searched until approved."""
    hunting: list[HuntingItem]
    """Open requests that are searched automatically."""
    grabbed: list[GrabbedItem]
    """Latest automatic downloads, newest first."""


def _aware(when: datetime) -> datetime:
    # SQLite hands datetimes back without a timezone
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _requested_books(session: Session, username: str | None) -> list[Audiobook]:
    asins = select(AudiobookRequest.asin)
    if username:
        asins = asins.where(AudiobookRequest.user_username == username)
    return list(
        session.exec(
            select(Audiobook)
            .where(col(Audiobook.asin).in_(asins))
            .options(
                selectinload(
                    cast(
                        InstrumentedAttribute[list[AudiobookRequest]],
                        cast(object, Audiobook.requests),
                    )
                )
            )
        ).all()
    )


def _auto_usernames(session: Session) -> set[str]:
    """Users whose requests are searched without an approval."""
    return set(
        session.exec(
            select(User.username).where(
                col(User.group).in_([GroupEnum.trusted, GroupEnum.admin])
            )
        ).all()
    )


def get_rows(session: Session, username: str | None = None) -> list[StageRow]:
    """Every requested book with its stage. With a username, only that user's."""
    auto_users = _auto_usernames(session)
    approved = retry_config.get_approved(session)
    state = retry_config.get_state(session)
    can_check = abs_config.is_valid(session) and abs_config.get_check_downloaded(
        session
    )
    stuck_before = datetime.now(UTC) - timedelta(
        days=retry_config.get_stuck_days(session)
    )

    rows: list[StageRow] = []
    for book in _requested_books(session, username):
        record = state.get(book.asin)
        by_user = any(r.user_username in auto_users for r in book.requests)
        stage: Stage
        stuck = False
        if book.fulfilled:
            waiting = bool(
                can_check and record and record.grabbed and not record.in_library_at
            )
            stage = "downloading" if waiting else "library"
            if waiting and record and record.grabbed:
                stuck = record.grabbed.when < stuck_before
        elif record and record.declined_at:
            stage = "declined"
        elif by_user or book.asin in approved:
            stage = "searching"
        else:
            stage = "pending"
        rows.append(
            StageRow(
                result=AudiobookWishlistResult(book=book, requests=book.requests),
                stage=stage,
                record=record,
                approved_only=stage == "searching" and not by_user,
                stuck=stuck,
                requested_at=min(
                    (_aware(r.updated_at) for r in book.requests),
                    default=datetime.now(UTC),
                ),
            )
        )
    return rows


def count_stages(rows: list[StageRow]) -> dict[Stage, int]:
    counts: dict[Stage, int] = dict.fromkeys(STAGES, 0)
    for row in rows:
        counts[row.stage] += 1
    return counts


def get_overview(session: Session) -> RetryOverview:
    rows = get_rows(session)
    grabbed = sorted(
        (
            (r.grabbed.when, GrabbedItem(key=k, record=r))
            for k, r in retry_config.get_state(session).items()
            if r.grabbed
        ),
        key=lambda g: g[0],
        reverse=True,
    )
    return RetryOverview(
        pending=[r.result for r in rows if r.stage == "pending"],
        hunting=[
            HuntingItem(result=r.result, approved_only=r.approved_only, record=r.record)
            for r in rows
            if r.stage == "searching"
        ],
        grabbed=[item for _, item in grabbed[:25]],
    )


def open_keys(session: Session) -> set[str]:
    """Books whose history must be kept: everything requested and not fulfilled."""
    return {
        r.result.book.asin for r in get_rows(session) if not r.result.book.fulfilled
    }
