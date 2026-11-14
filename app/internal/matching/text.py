"""Text helpers for comparing book titles and names. No app imports."""

import re
import unicodedata

_CENSORED = re.compile(r"\w[-*]+\w")
_ROLE_SUFFIX = re.compile(r"\s+-\s+")
_SUBTITLE = re.compile(r"\s+-\s+|:\s")


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


def full_title_regex(title: str | None) -> re.Pattern[str] | None:
    """Like title_regex, but for the whole title including what follows a colon
    or dash. Returns None when that is no longer than the core title."""
    words = norm(title).split()
    if len(words) <= len(norm(core_title(title)).split()):
        return None
    return re.compile(" " + " ".join(re.escape(w) for w in words) + " ")


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
