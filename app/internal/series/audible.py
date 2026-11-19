"""Lists the books of a series from the Audible catalog.

A series is a catalog product of its own. Its `relationships` name the books
that belong to it, which are then fetched in batches. If that yields nothing,
the series is put together from a keyword search for its title, keeping the
results that say they belong to it.
"""

import re
import time
from datetime import UTC, datetime
from typing import Literal, cast

from aiohttp import ClientSession
from pydantic import BaseModel, ValidationError

from app.internal.audible.types import (
    AudibleProduct,
    audible_region_type,
    audible_regions,
    get_region_from_settings,
    region_languages,
)
from app.internal.models import Audiobook
from app.util.log import logger

# How long a listed series is reused for page views. The daily check always
# lists it again.
SERIES_TTL = 60 * 60 * 6
BATCH = 50
SEARCH_PAGES = 4

_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
_RANGE = re.compile(r"\d\s*(?:-|–|—|to|&|and|,)\s*\d", re.IGNORECASE)
_OMNIBUS = re.compile(
    r"\b(?:omnibus|box(?:ed)?\s*set|bundle|complete\s+series"
    + r"|(?:books?|volumes?|vols?\.?|parts?)\s+\d+\s*(?:-|–|—|to|&|and|,)\s*\d+)\b",
    re.IGNORECASE,
)
_DRAMATIZED = re.compile(r"dramati[sz]ed|graphic\s*audio", re.IGNORECASE)


def api_base(region: audible_region_type) -> str:
    return f"https://api.audible{audible_regions[region]}/1.0"


class SeriesBook(BaseModel):
    asin: str
    title: str
    subtitle: str | None = None
    authors: list[str] = []
    narrators: list[str] = []
    cover_image: str | None = None
    release_date: datetime
    runtime_length_min: int = 0
    language: str | None = None
    format_type: str | None = None
    sequence: str | None = None

    @property
    def number(self) -> float | None:
        """Position in the series, when it is a single number ('3', '2.5')."""
        match = _NUMBER.match(self.sequence or "")
        return float(match.group(1)) if match else None

    @property
    def number_text(self) -> str:
        number = self.number
        if number is None:
            return (self.sequence or "").strip()
        return str(int(number)) if number == int(number) else str(number)

    @property
    def is_omnibus(self) -> bool:
        """Several books in one: 'Books 1-3', a box set, an omnibus."""
        if self.sequence and self.number is None and _RANGE.search(self.sequence):
            return True
        return bool(_OMNIBUS.search(f"{self.title} {self.subtitle or ''}"))

    @property
    def is_dramatized(self) -> bool:
        return bool(_DRAMATIZED.search(f"{self.title} {self.subtitle or ''}"))

    @property
    def is_abridged(self) -> bool:
        return (self.format_type or "").lower() == "abridged"

    def released(self, now: datetime | None = None) -> bool:
        return self.release_date <= (now or datetime.now(UTC))

    @property
    def release_text(self) -> str:
        return self.release_date.strftime("%b %d, %Y")

    def in_language_of(self, region: str) -> bool:
        allowed = region_languages.get(cast(audible_region_type, region))
        if not allowed or not self.language:
            return True
        return self.language.lower() in allowed

    def to_audiobook(self) -> Audiobook:
        return Audiobook(
            asin=self.asin,
            title=self.title,
            subtitle=self.subtitle,
            authors=self.authors,
            narrators=self.narrators,
            cover_image=self.cover_image,
            release_date=self.release_date,
            runtime_length_min=self.runtime_length_min,
        )


class SeriesInfo(BaseModel):
    asin: str
    title: str
    region: str
    books: list[SeriesBook]
    source: Literal["relationships", "search"]
    """How the list was put together."""

    @property
    def cover_image(self) -> str | None:
        for book in self.books:
            if book.cover_image:
                return book.cover_image
        return None

    @property
    def authors(self) -> list[str]:
        seen: dict[str, int] = {}
        for book in self.books:
            for author in book.authors[:2]:
                seen[author] = seen.get(author, 0) + 1
        return sorted(seen, key=lambda a: -seen[a])[:2]


_cache: dict[tuple[str, str], tuple[float, SeriesInfo]] = {}


def clear_cache() -> None:
    _cache.clear()


def _sort_key(book: SeriesBook) -> tuple[int, float, datetime]:
    """Reading order: numbered books, then box sets, then the unnumbered."""
    number = book.number
    if number is not None:
        return (0, number, book.release_date)
    first = re.search(r"\d+(?:\.\d+)?", book.sequence or "")
    if first:
        return (1, float(first.group(0)), book.release_date)
    return (2, 0.0, book.release_date)


def _to_book(product: AudibleProduct, sequence: str | None) -> SeriesBook:
    book = product.to_audiobook()
    return SeriesBook(
        asin=book.asin,
        title=book.title,
        subtitle=book.subtitle,
        authors=book.authors,
        narrators=book.narrators,
        cover_image=book.cover_image,
        release_date=book.release_date,
        runtime_length_min=book.runtime_length_min,
        language=product.language,
        format_type=product.format_type,
        sequence=(sequence or "").strip() or None,
    )


def _products(payload: object, key: str = "products") -> list[AudibleProduct]:
    """Reads the products of a catalog answer, skipping entries that are not
    complete books (no longer sold in the region, for example)."""
    if not isinstance(payload, dict):
        return []
    raw = cast(dict[str, object], payload).get(key)
    if not isinstance(raw, list):
        return []
    out: list[AudibleProduct] = []
    for entry in cast(list[object], raw):
        try:
            out.append(AudibleProduct.model_validate(entry))
        except ValidationError:
            continue
    return out


async def _children(
    client_session: ClientSession, series_asin: str, region: audible_region_type
) -> tuple[str | None, dict[str, str | None]]:
    """The series' own title and its books as {asin: sequence}."""
    url = f"{api_base(region)}/catalog/products/{series_asin}"
    params = {"response_groups": "relationships,product_attrs"}
    async with client_session.get(url, params=params) as response:
        response.raise_for_status()
        payload = cast(object, await response.json())
    if not isinstance(payload, dict):
        return None, {}
    product = cast(dict[str, object], payload).get("product")
    if not isinstance(product, dict):
        return None, {}
    product = cast(dict[str, object], product)
    title = product.get("title")
    books: dict[str, str | None] = {}
    relationships = product.get("relationships")
    if isinstance(relationships, list):
        for rel in cast(list[object], relationships):
            if not isinstance(rel, dict):
                continue
            rel = cast(dict[str, object], rel)
            asin = rel.get("asin")
            if rel.get("relationship_to_product") != "child" or not isinstance(
                asin, str
            ):
                continue
            sequence = rel.get("sequence")
            books[asin] = sequence if isinstance(sequence, str) else None
    return (title if isinstance(title, str) else None), books


async def _by_asins(
    client_session: ClientSession,
    asins: list[str],
    region: audible_region_type,
) -> list[AudibleProduct]:
    url = f"{api_base(region)}/catalog/products"
    found: list[AudibleProduct] = []
    for start in range(0, len(asins), BATCH):
        batch = asins[start : start + BATCH]
        params = {
            "asins": ",".join(batch),
            "num_results": str(BATCH),
            "response_groups": "media,product_attrs,series",
        }
        async with client_session.get(url, params=params) as response:
            response.raise_for_status()
            found.extend(_products(cast(object, await response.json())))
    return found


async def _by_search(
    client_session: ClientSession,
    series_asin: str,
    title: str,
    region: audible_region_type,
) -> list[SeriesBook]:
    url = f"{api_base(region)}/catalog/products"
    books: dict[str, SeriesBook] = {}
    for page in range(SEARCH_PAGES):
        params = {
            "keywords": title,
            "num_results": str(BATCH),
            "page": str(page),
            "products_sort_by": "Relevance",
            "response_groups": "media,product_attrs,series",
        }
        async with client_session.get(url, params=params) as response:
            response.raise_for_status()
            products = _products(cast(object, await response.json()))
        before = len(books)
        for product in products:
            ref = product.series_ref(series_asin)
            if ref:
                books[product.asin] = _to_book(product, ref.sequence)
        if len(products) < BATCH or (page and len(books) == before):
            break
    return list(books.values())


async def get_series(
    client_session: ClientSession,
    series_asin: str,
    region: audible_region_type | None = None,
    title_hint: str | None = None,
    force: bool = False,
) -> SeriesInfo | None:
    """Every book of a series, in reading order. None if nothing was found."""
    if region is None:
        region = get_region_from_settings()
    key = (series_asin, region)
    hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < SERIES_TTL:
        return hit[1]

    title: str | None = None
    books: list[SeriesBook] = []
    source: Literal["relationships", "search"] = "relationships"
    try:
        title, children = await _children(client_session, series_asin, region)
        if children:
            for product in await _by_asins(client_session, list(children), region):
                if product.asin not in children:
                    # never trust an answer that holds more than was asked for
                    continue
                ref = product.series_ref(series_asin)
                sequence = (ref.sequence if ref else None) or children.get(product.asin)
                books.append(_to_book(product, sequence))
                if ref and not title:
                    title = ref.title
    except Exception as e:
        logger.warning(
            "Series: could not list the series by its relationships",
            series=series_asin,
            region=region,
            error=str(e),
        )
        books = []

    if not books and (title or title_hint):
        source = "search"
        try:
            books = await _by_search(
                client_session, series_asin, title or title_hint or "", region
            )
        except Exception as e:
            logger.error(
                "Series: could not find the series by searching",
                series=series_asin,
                region=region,
                error=str(e),
            )
            return None

    if not books:
        logger.warning("Series: no books found", series=series_asin, region=region)
        return None

    info = SeriesInfo(
        asin=series_asin,
        title=title or title_hint or "Series",
        region=region,
        books=sorted(books, key=_sort_key),
        source=source,
    )
    logger.info(
        "Series listed",
        series=series_asin,
        title=info.title,
        books=len(info.books),
        source=source,
    )
    _cache[key] = (time.time(), info)
    return info
