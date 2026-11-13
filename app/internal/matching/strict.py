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
import unicodedata

from app.internal.models import Audiobook, ManualBookRequest, ProwlarrSource

type Book = Audiobook | ManualBookRequest

_CENSORED = re.compile(r"\w[-*]+\w")
_ROLE_SUFFIX = re.compile(r"\s+-\s+")
_SUBTITLE = re.compile(r"\s+-\s+|:\s")
_BY = re.compile(r"\s+by\s+")


def norm(text: str | None) -> str:
    """Lowercase, fold accents (Phönix -> phonix), punctuation to spaces.

    The result is padded with a space on both sides so that `f" {word} " in`
    only matches whole words.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ß", "ss").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {' '.join(text.split())} "


def core_title(title: str | None) -> str:
    """Title without its subtitle.

    'Yesteryear: A GMA Book Club Pick' -> 'Yesteryear'
    'Harry Potter ... - Gesprochen von Rufus Beck' -> 'Harry Potter ...'
    """
    title = title or ""
    return _SUBTITLE.split(title, maxsplit=1)[0].strip() or title


def person(name: str | None) -> str:
    """Name without an ' - illustrator' style role suffix."""
    return _ROLE_SUFFIX.split(name or "", maxsplit=1)[0].strip()


def surnames(people: list[str] | None) -> list[str]:
    """Normalized last word of each name."""
    out: list[str] = []
    for p in people or []:
        parts = norm(person(p)).split()
        if parts and len(parts[-1]) > 1:
            out.append(parts[-1])
    return out


def title_regex(title: str | None) -> re.Pattern[str] | None:
    """Whole-phrase matcher for a normalized haystack.

    A censored word such as 'F--k' or 'Sh*t' matches any word with the same
    first and last letters. Returns None when the title has no usable words.
    """
    parts: list[str] = []
    for word in core_title(title).split():
        if _CENSORED.search(word):
            bits = [norm(b).strip().replace(" ", "") for b in re.split(r"[-*]+", word)]
            parts.append("[a-z0-9]*".join(re.escape(b) for b in bits))
        else:
            # one word can normalize into several ("Friend's" -> "friend s")
            parts.extend(re.escape(w) for w in norm(word).split())
    if not parts:
        return None
    return re.compile(" " + " ".join(parts) + " ")


def query_title(title: str | None) -> str:
    """Search-friendly title: subtitle, censored words and punctuation dropped.

    Words with apostrophes ("Friend's") are dropped as well, since indexers
    tokenize them unpredictably. The strict match on the results still checks
    the full title.
    """
    words: list[str] = []
    for w in core_title(title).split():
        if _CENSORED.search(w) or re.search(r"['’]", w):
            continue
        w = re.sub(r"[^\w\s-]", "", w).strip("-")
        if w:
            words.append(w)
    return " ".join(words)


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
    authors = surnames(book.authors)
    narrators = surnames(book.narrators)
    if not title or title_re is None or not authors:
        return []

    matches: list[tuple[tuple[bool, int, int], ProwlarrSource]] = []
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

        # Prefer the closest edition: fewest narrators that were not asked for
        # (full-cast versions lose), then fewest extra words in the release
        # title ("... (Hörbuch mit voller Besetzung)" loses).
        extra_narrators = len([n for n in release_narrators if n not in narrators])
        release_title = norm(_BY.split(source.title, maxsplit=1)[0])
        extra_words = max(0, len(release_title.split()) - len(title.split()))
        matches.append(((narrator_ok, -extra_narrators, -extra_words), source))

    # sort() is stable, so equally close editions keep their ranked order
    matches.sort(key=lambda m: m[0], reverse=True)
    return [source for _, source in matches]
