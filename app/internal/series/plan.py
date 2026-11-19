"""Decides which books of a watched series to ask for. Pure logic, no I/O."""

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel

from app.internal.series.audible import SeriesBook
from app.internal.series.config import SeriesMode


class PlanItem(BaseModel):
    book: SeriesBook
    reason: str


class SeriesPlan(BaseModel):
    request: list[SeriesBook] = []
    """Missing books that would be asked for, in reading order."""
    have: list[PlanItem] = []
    """Books that are in the library or already requested."""
    skipped: list[PlanItem] = []
    upcoming: list[SeriesBook] = []
    """Announced, not out yet."""

    @property
    def upcoming_text(self) -> str | None:
        if not self.upcoming:
            return None
        book = min(self.upcoming, key=lambda b: b.release_date)
        label = f"Book {book.number_text}" if book.number_text else book.title
        return f"{label}, {book.release_text}"


def _edition_rank(
    book: SeriesBook, usual_narrators: set[str]
) -> tuple[int, int, int, int, datetime]:
    """Lower is better: the edition most like the ones already owned."""
    same_narrator = any(n in usual_narrators for n in book.narrators)
    return (
        0 if same_narrator or not usual_narrators else 1,
        1 if book.is_abridged else 0,
        1 if book.is_dramatized else 0,
        -book.runtime_length_min,
        book.release_date,
    )


def build_plan(
    books: list[SeriesBook],
    mode: SeriesMode,
    in_library: Callable[[SeriesBook], bool],
    requested: Callable[[SeriesBook], bool],
    watch_since: datetime,
    language_region: str | None = None,
    now: datetime | None = None,
) -> SeriesPlan:
    """`language_region`: skip editions in a language that store does not speak."""
    now = now or datetime.now(UTC)
    plan = SeriesPlan()

    have_numbers: set[float] = set()
    narrators: Counter[str] = Counter()
    rest: list[SeriesBook] = []
    for book in books:
        owned = in_library(book)
        if owned or requested(book):
            plan.have.append(
                PlanItem(book=book, reason="in library" if owned else "requested")
            )
            if book.number is not None:
                have_numbers.add(book.number)
            narrators.update(book.narrators)
        else:
            rest.append(book)

    # the narrators most of the owned books share
    usual: set[str] = set()
    if narrators:
        top = narrators.most_common(1)[0][1]
        usual = {name for name, count in narrators.items() if count == top}

    newest_owned = max(have_numbers) if have_numbers else None

    candidates: dict[float, list[SeriesBook]] = {}
    for book in rest:
        if language_region and not book.in_language_of(language_region):
            plan.skipped.append(PlanItem(book=book, reason="other language"))
        elif book.is_omnibus:
            plan.skipped.append(PlanItem(book=book, reason="omnibus or box set"))
        elif book.number is None:
            plan.skipped.append(PlanItem(book=book, reason="not numbered"))
        elif book.number in have_numbers:
            plan.skipped.append(
                PlanItem(
                    book=book,
                    reason=f"another edition of book {book.number_text}",
                )
            )
        elif not book.released(now):
            plan.upcoming.append(book)
        elif mode == "new" and newest_owned is not None and book.number < newest_owned:
            plan.skipped.append(
                PlanItem(book=book, reason="before your newest book (New only)")
            )
        elif (
            mode == "new" and newest_owned is None and book.release_date <= watch_since
        ):
            plan.skipped.append(
                PlanItem(
                    book=book,
                    reason="released before the series was watched (New only)",
                )
            )
        else:
            candidates.setdefault(book.number, []).append(book)

    for number in sorted(candidates):
        editions = sorted(candidates[number], key=lambda b: _edition_rank(b, usual))
        plan.request.append(editions[0])
        for other in editions[1:]:
            plan.skipped.append(
                PlanItem(
                    book=other,
                    reason=f"another edition of book {other.number_text}",
                )
            )
    return plan
