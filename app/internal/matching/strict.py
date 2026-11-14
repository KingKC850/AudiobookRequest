"""Strict release matching for automatic downloads.

The default ranking is fuzzy: it sorts every result and the best one wins, even
when nothing really matches (a request for "Day" by Elie Wiesel can end up with
a book written by somebody named Day). That is fine for a list a human picks
from, but not for a download nobody reviews.

A strict match requires the release to contain BOTH the whole core title as a
phrase and the surname of one of the authors. A release that names a different
narrator than the requested one is a different edition and never matches.
"""

import re

from app.internal.matching.text import (
    core_title,
    full_title_regex,
    norm,
    person,
    query_title,
    surnames,
    title_regex,
)
from app.internal.models import Audiobook, ManualBookRequest, ProwlarrSource

__all__ = [
    "Book",
    "core_title",
    "norm",
    "person",
    "query_title",
    "strict_matches",
    "surnames",
    "title_regex",
]

type Book = Audiobook | ManualBookRequest

_BY = re.compile(r"\s+by\s+")


def strict_matches(
    book: Book,
    sources: list[ProwlarrSource],
    *,
    require_narrator: bool = False,
    narrator_in_query: bool = False,
    min_seeders: int = 0,
) -> list[ProwlarrSource]:
    """Returns the sources that strictly match the book, best first.

    `sources` should already be in ranked order: that order is kept between
    releases that are equally close to the requested edition.

    narrator_in_query: the search itself was filtered by narrator, so results
    without narrator metadata are treated as that narrator's edition.
    """
    title = norm(core_title(book.title)).strip()
    title_re = title_regex(book.title)
    full_re = full_title_regex(book.title)
    authors = surnames(book.authors)
    narrators = surnames(book.narrators)
    if not title or title_re is None or not authors:
        return []

    matches: list[tuple[tuple[bool, bool, int, int], ProwlarrSource]] = []
    for source in sources:
        meta = source.book_metadata
        hay = norm(" ".join([source.title, *meta.authors, *meta.narrators]))
        if not title_re.search(hay):
            continue
        if not any(f" {a} " in hay for a in authors):
            continue

        release_narrators = surnames(meta.narrators)
        narrator_ok = any(f" {n} " in hay for n in narrators)
        if narrator_in_query and not meta.narrators:
            narrator_ok = True
        if narrators and not narrator_ok:
            # The indexer says who reads this release and it is someone else:
            # a different edition. Never grab it.
            if release_narrators:
                continue
            # Narrator unknown: only allowed when a narrator is not required.
            if require_narrator:
                continue

        if source.protocol == "torrent" and source.seeders < min_seeders:
            continue

        # A title like "Star Wars: Thrawn" shortens to "Star Wars", which every
        # book of that series matches. A release naming the whole title wins.
        whole_title = bool(full_re and full_re.search(hay))

        # Then prefer the closest edition: fewest narrators that were not asked for
        # (full-cast versions lose), then fewest extra words in the release
        # title ("... (Hörbuch mit voller Besetzung)" loses).
        extra_narrators = len([n for n in release_narrators if n not in narrators])
        release_title = norm(_BY.split(source.title, maxsplit=1)[0])
        extra_words = max(0, len(release_title.split()) - len(title.split()))
        matches.append(
            ((whole_title, narrator_ok, -extra_narrators, -extra_words), source)
        )

    # sort() is stable, so equally close editions keep their ranked order
    matches.sort(key=lambda m: m[0], reverse=True)
    return [source for _, source in matches]
