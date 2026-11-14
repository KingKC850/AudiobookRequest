"""What is in the Audiobookshelf library, kept in memory. No app model imports."""

import time

from app.internal.matching.text import core_title, norm, surnames


class LibraryIndex:
    def __init__(self) -> None:
        self.asins: set[str] = set()
        # normalized full title -> normalized author strings of the items with it
        self.titles: dict[str, list[str]] = {}
        self.loaded_at: float | None = None

    @property
    def loaded(self) -> bool:
        return self.loaded_at is not None

    def clear(self) -> None:
        self.asins = set()
        self.titles = {}
        self.loaded_at = None

    def load(self, items: list[tuple[str | None, str | None, str | None]]) -> None:
        """items: (asin, title, author names) of every book in the library."""
        asins: set[str] = set()
        titles: dict[str, list[str]] = {}
        for asin, title, author in items:
            if asin and asin.strip():
                asins.add(asin.strip().upper())
            # The full title, never a shortened one: "Mistborn: The Final Empire"
            # must not make every other "Mistborn: ..." book look owned.
            key = norm(title).strip()
            if key:
                titles.setdefault(key, []).append(norm(author))
        self.asins = asins
        self.titles = titles
        self.loaded_at = time.time()

    def owns(self, asin: str | None, title: str | None, authors: list[str]) -> bool:
        if not self.loaded:
            return False
        if asin and asin.upper() in self.asins:
            return True
        # No Audible id on the library item: same title and one author surname
        # the library often drops the subtitle that Audible keeps in the title
        candidates = self.titles.get(norm(title).strip()) or self.titles.get(
            norm(core_title(title)).strip()
        )
        if not candidates:
            return False
        names = surnames(authors)
        if not names:
            return False
        return any(f" {name} " in author for author in candidates for name in names)


library_index = LibraryIndex()
