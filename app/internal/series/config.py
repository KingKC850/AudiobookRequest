"""Settings and the watch list for series.

Everything is kept as JSON in the Config table, so no migration is needed.
"""

from datetime import UTC, datetime
from typing import Literal, final

from pydantic import BaseModel, TypeAdapter
from sqlmodel import Session

from app.util.cache import StringConfigCache

SeriesMode = Literal["new", "fill"]
"""new: only books after the newest one already owned.
fill: every missing book of the series."""

SeriesConfigKey = Literal[
    "series_enabled",
    "series_default_mode",
    "series_run_hour",
    "series_max_per_run",
    "series_user",
    "series_watch",
    "series_last_run",
    "search_language_filter",
]

DEFAULT_SERIES_USER = "series-watch"
# How many requested books are remembered per series.
KEEP_REQUESTED = 60


def _local(when: datetime) -> str:
    return when.astimezone().strftime("%b %d, %I:%M %p")


class RequestedBook(BaseModel):
    asin: str
    title: str
    sequence: str | None = None
    when: datetime


class WatchedSeries(BaseModel):
    asin: str
    title: str
    region: str
    mode: SeriesMode
    added_by: str
    added_at: datetime
    confirmed: bool = False
    """Nothing is requested until someone has looked at the preview and agreed."""
    cover_image: str | None = None
    last_checked: datetime | None = None
    last_result: str | None = None
    upcoming: str | None = None
    """The next book that is announced but not out yet."""
    requested: list[RequestedBook] = []

    @property
    def mode_text(self) -> str:
        return "Fill in" if self.mode == "fill" else "New only"

    @property
    def last_checked_text(self) -> str:
        return _local(self.last_checked) if self.last_checked else "never"


_Watch = TypeAdapter(dict[str, WatchedSeries])


@final
class SeriesConfig(StringConfigCache[SeriesConfigKey]):
    # -- settings ----------------------------------------------------------
    def _bool(self, session: Session, key: SeriesConfigKey, default: bool) -> bool:
        value = self.get_bool(session, key)
        return default if value is None else value

    def get_enabled(self, session: Session) -> bool:
        """Check watched series once a day and request what is missing."""
        return self._bool(session, "series_enabled", True)

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "series_enabled", enabled)

    def get_default_mode(self, session: Session) -> SeriesMode:
        return "fill" if self.get(session, "series_default_mode") == "fill" else "new"

    def set_default_mode(self, session: Session, mode: SeriesMode):
        self.set(session, "series_default_mode", mode)

    def get_run_hour(self, session: Session) -> int:
        """Hour of the day (server time) the daily check runs at."""
        return min(23, max(0, self.get_int(session, "series_run_hour", 4)))

    def set_run_hour(self, session: Session, hour: int):
        self.set_int(session, "series_run_hour", min(23, max(0, hour)))

    def get_max_per_run(self, session: Session) -> int:
        """Most books requested per series in one daily check. 0 means no limit."""
        return self.get_int(session, "series_max_per_run", 5)

    def set_max_per_run(self, session: Session, max_per_run: int):
        self.set_int(session, "series_max_per_run", max(0, max_per_run))

    def get_user(self, session: Session) -> str:
        """The account series requests are made under."""
        return self.get(session, "series_user") or DEFAULT_SERIES_USER

    def set_user(self, session: Session, username: str):
        self.set(session, "series_user", username.strip() or DEFAULT_SERIES_USER)

    def get_language_filter(self, session: Session) -> bool:
        """Hide editions in a language the selected Audible store does not speak."""
        return self._bool(session, "search_language_filter", True)

    def set_language_filter(self, session: Session, enabled: bool):
        self.set_bool(session, "search_language_filter", enabled)

    def get_last_run(self, session: Session) -> datetime | None:
        raw = self.get(session, "series_last_run")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def set_last_run(self, session: Session, when: datetime):
        self.set(session, "series_last_run", when.isoformat())

    # -- watch list --------------------------------------------------------
    def get_watch(self, session: Session) -> dict[str, WatchedSeries]:
        raw = self.get(session, "series_watch")
        if not raw:
            return {}
        return _Watch.validate_json(raw)

    def set_watch(self, session: Session, watch: dict[str, WatchedSeries]):
        self.set(session, "series_watch", _Watch.dump_json(watch).decode())

    def get_series(self, session: Session, asin: str) -> WatchedSeries | None:
        return self.get_watch(session).get(asin)

    def put_series(self, session: Session, series: WatchedSeries):
        watch = self.get_watch(session)
        series.requested = series.requested[-KEEP_REQUESTED:]
        watch[series.asin] = series
        self.set_watch(session, watch)

    def remove_series(self, session: Session, asin: str) -> bool:
        watch = self.get_watch(session)
        if asin not in watch:
            return False
        del watch[asin]
        self.set_watch(session, watch)
        return True


series_config = SeriesConfig()


def now_utc() -> datetime:
    return datetime.now(UTC)
