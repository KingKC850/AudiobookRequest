"""Which series a book belongs to, kept in memory. No app model imports.

Audible names a book's series in its search results, but the book table has no
column for it. The answer is remembered here whenever a result passes through,
so a book card can link to its series without a schema change. It is only a
convenience: after a restart the links come back with the next search.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesRef:
    asin: str
    title: str
    sequence: str | None = None

    @property
    def label(self) -> str:
        if self.sequence:
            return f"{self.title} #{self.sequence}"
        return self.title


_MAX = 20000
_memo: dict[str, SeriesRef] = {}


def remember(book_asin: str, ref: SeriesRef) -> None:
    if len(_memo) >= _MAX and book_asin not in _memo:
        # drop the oldest entries; dicts keep insertion order
        for key in list(_memo)[: _MAX // 10]:
            del _memo[key]
    _memo[book_asin] = ref


def series_of(book_asin: str | None) -> SeriesRef | None:
    if not book_asin:
        return None
    return _memo.get(book_asin)


def clear() -> None:
    _memo.clear()
