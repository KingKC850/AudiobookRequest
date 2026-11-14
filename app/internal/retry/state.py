"""Settings and per-book history for automatic downloads and their retries.

Everything is kept as JSON in the Config table, so no migration is needed.
"""

from datetime import UTC, datetime
from typing import Literal, final

from pydantic import BaseModel, TypeAdapter
from sqlmodel import Session

from app.util.cache import StringConfigCache

RetryConfigKey = Literal[
    "retry_enabled",
    "retry_min_hours",
    "retry_delay_seconds",
    "retry_max_per_run",
    "retry_stuck_days",
    "retry_state",
    "retry_approved",
]

# How many finished (grabbed) records are kept for the "recently grabbed" list.
KEEP_GRABBED = 50


def _local(when: datetime) -> str:
    return when.astimezone().strftime("%b %d, %I:%M %p")


class GrabInfo(BaseModel):
    release: str
    indexer: str
    info_url: str | None = None
    query: str | None = None
    """The fallback query that found the release. None if the normal search did."""
    when: datetime

    @property
    def when_text(self) -> str:
        return _local(self.when)


class RetryRecord(BaseModel):
    title: str | None = None
    first_seen: datetime
    tries: int = 0
    last_tried: datetime | None = None
    last_result: str | None = None
    grabbed: GrabInfo | None = None
    in_library_at: datetime | None = None
    """When the grabbed book showed up in the library."""
    declined_at: datetime | None = None
    """An admin declined the request. The reason is the book's admin note."""

    @property
    def last_tried_text(self) -> str:
        return _local(self.last_tried) if self.last_tried else "never"

    @property
    def in_library_text(self) -> str:
        return _local(self.in_library_at) if self.in_library_at else ""


_State = TypeAdapter(dict[str, RetryRecord])
_Approved = TypeAdapter(list[str])


@final
class RetryConfig(StringConfigCache[RetryConfigKey]):
    # -- settings ----------------------------------------------------------
    def get_enabled(self, session: Session) -> bool:
        """Periodically search again for open requests that were not found."""
        return bool(self.get_bool(session, "retry_enabled"))

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "retry_enabled", enabled)

    def get_min_hours(self, session: Session) -> int:
        """Do not search for the same book more often than this."""
        return self.get_int(session, "retry_min_hours", 20)

    def set_min_hours(self, session: Session, min_hours: int):
        self.set_int(session, "retry_min_hours", max(1, min_hours))

    def get_delay_seconds(self, session: Session) -> int:
        """Pause between two books, to go easy on the indexers."""
        return self.get_int(session, "retry_delay_seconds", 20)

    def set_delay_seconds(self, session: Session, delay_seconds: int):
        self.set_int(session, "retry_delay_seconds", max(0, delay_seconds))

    def get_max_per_run(self, session: Session) -> int:
        """Most books searched in one run. 0 means no limit."""
        return self.get_int(session, "retry_max_per_run", 0)

    def set_max_per_run(self, session: Session, max_per_run: int):
        self.set_int(session, "retry_max_per_run", max(0, max_per_run))

    def get_stuck_days(self, session: Session) -> int:
        """A grabbed book missing from the library for this long is flagged."""
        return self.get_int(session, "retry_stuck_days", 3)

    def set_stuck_days(self, session: Session, stuck_days: int):
        self.set_int(session, "retry_stuck_days", max(1, stuck_days))

    # -- approved books ----------------------------------------------------
    def get_approved(self, session: Session) -> set[str]:
        """Books an admin approved for automatic searching, by ASIN."""
        raw = self.get(session, "retry_approved")
        if not raw:
            return set()
        return set(_Approved.validate_json(raw))

    def set_approved(self, session: Session, approved: set[str]):
        self.set(
            session, "retry_approved", _Approved.dump_json(sorted(approved)).decode()
        )

    def approve(self, session: Session, *keys: str):
        self.set_approved(session, self.get_approved(session) | set(keys))

    def unapprove(self, session: Session, *keys: str):
        self.set_approved(session, self.get_approved(session) - set(keys))

    # -- history -----------------------------------------------------------
    def get_state(self, session: Session) -> dict[str, RetryRecord]:
        raw = self.get(session, "retry_state")
        if not raw:
            return {}
        return _State.validate_json(raw)

    def set_state(self, session: Session, state: dict[str, RetryRecord]):
        self.set(session, "retry_state", _State.dump_json(state).decode())

    def record_attempt(
        self,
        session: Session,
        key: str,
        title: str | None,
        result: str,
        grabbed: GrabInfo | None = None,
    ):
        """Remember the outcome of one automatic download attempt for a book."""
        now = datetime.now(UTC)
        state = self.get_state(session)
        record = state.get(key) or RetryRecord(first_seen=now)
        record.title = title
        record.tries += 1
        record.last_tried = now
        record.last_result = result
        if grabbed:
            record.grabbed = grabbed
        state[key] = record
        self.set_state(session, state)

    def _update(self, session: Session, keys: tuple[str, ...], **changes: object):
        state = self.get_state(session)
        now = datetime.now(UTC)
        for key in keys:
            record = state.get(key) or RetryRecord(first_seen=now)
            state[key] = record.model_copy(update=changes)
        self.set_state(session, state)

    def mark_in_library(self, session: Session, *keys: str):
        self._update(session, keys, in_library_at=datetime.now(UTC))

    def set_declined(self, session: Session, key: str, declined: bool):
        self._update(
            session, (key,), declined_at=datetime.now(UTC) if declined else None
        )

    def clear_grab(self, session: Session, key: str):
        """Forget a grab, so the book counts as not found yet."""
        self._update(session, (key,), grabbed=None, in_library_at=None)

    def prune(self, session: Session, open_keys: set[str]):
        """Forget books that are no longer open, except the latest grabs."""
        state = self.get_state(session)
        closed = [(k, r) for k, r in state.items() if k not in open_keys]
        grabbed = sorted(
            ((r.grabbed.when, k) for k, r in closed if r.grabbed),
            reverse=True,
        )
        keep = {k for _, k in grabbed[:KEEP_GRABBED]}
        pruned = {k: r for k, r in state.items() if k in open_keys or k in keep}
        if len(pruned) != len(state):
            self.set_state(session, pruned)

        approved = self.get_approved(session)
        if not approved <= open_keys:
            self.set_approved(session, approved & open_keys)


retry_config = RetryConfig()
