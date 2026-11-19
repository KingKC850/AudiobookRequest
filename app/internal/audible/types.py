from abc import ABCMeta, abstractmethod
from datetime import UTC, datetime
from typing import Literal, override

from pydantic import BaseModel

from app.internal.env_settings import Settings
from app.internal.models import Audiobook
from app.internal.series.memo import SeriesRef, remember

REFETCH_TTL = 60 * 60 * 24 * 7  # 1 week

audible_region_type = Literal[
    "us",
    "ca",
    "uk",
    "au",
    "fr",
    "de",
    "jp",
    "it",
    "in",
    "es",
    "br",
]
audible_regions: dict[audible_region_type, str] = {
    "us": ".com",
    "ca": ".ca",
    "uk": ".co.uk",
    "au": ".com.au",
    "fr": ".fr",
    "de": ".de",
    "jp": ".co.jp",
    "it": ".it",
    "in": ".in",
    "es": ".es",
    "br": ".com.br",
}


def get_region_from_settings() -> audible_region_type:
    region = Settings().app.default_region
    if region not in audible_regions:
        return "us"
    return region


class AudibleProduct(BaseModel):
    class _Author(BaseModel):
        name: str

    class _Series(BaseModel):
        asin: str | None = None
        title: str | None = None
        sequence: str | None = None

    asin: str
    authors: list[_Author] = []
    narrators: list[_Author] = []
    product_images: dict[str, str] = {}
    runtime_length_min: int = 0
    release_date: str
    title: str
    subtitle: str | None = None
    language: str | None = None
    """Only present when the `product_attrs` response group is requested."""
    format_type: str | None = None
    """'unabridged' or 'abridged'. Only with the `product_attrs` group."""
    series: list[_Series] | None = None
    """Only present when the `series` response group is requested."""

    def series_ref(self, series_asin: str | None = None) -> SeriesRef | None:
        """The series the book is part of. With an ASIN, only that series."""
        for entry in self.series or []:
            if not entry.asin or not entry.title:
                continue
            if series_asin is None or entry.asin == series_asin:
                return SeriesRef(
                    asin=entry.asin,
                    title=entry.title,
                    sequence=(entry.sequence or "").strip() or None,
                )
        return None

    def to_audiobook(self) -> Audiobook:
        cover_image = self.product_images.get("500")
        if not cover_image:
            covers = list(self.product_images.values())
            if covers:  # default to first cover other than the one keyed by "500"
                cover_image = covers[0]

        release_date = datetime.fromisoformat(self.release_date)
        if release_date.tzinfo is None:
            release_date = release_date.replace(tzinfo=UTC)

        ref = self.series_ref()
        if ref:
            remember(self.asin, ref)

        return Audiobook(
            asin=self.asin,
            title=self.title,
            subtitle=self.subtitle,
            authors=[author.name for author in self.authors],
            narrators=[narrator.name for narrator in self.narrators],
            cover_image=cover_image,
            release_date=release_date,
            runtime_length_min=self.runtime_length_min,
        )


# Languages each Audible store is expected to show. A store's catalog also lists
# editions in other languages (the US store has German translations, for
# example), which only add noise to search results.
region_languages: dict[audible_region_type, set[str]] = {
    "us": {"english"},
    "ca": {"english", "french"},
    "uk": {"english"},
    "au": {"english"},
    "fr": {"french"},
    "de": {"german"},
    "jp": {"japanese"},
    "it": {"italian"},
    "in": {"english", "hindi"},
    "es": {"spanish"},
    "br": {"portuguese"},
}


def in_region_language(
    products: list[AudibleProduct], region: audible_region_type
) -> list[AudibleProduct]:
    """Drops editions in a language the store's region does not speak.
    Products that do not say their language are kept."""
    allowed = region_languages.get(region)
    if not allowed:
        return products
    return [p for p in products if not p.language or p.language.lower() in allowed]


class _Response(BaseModel, metaclass=ABCMeta):
    @abstractmethod
    def audiobooks(self) -> list[Audiobook]:
        pass


class AudibleSearchResponse(_Response):
    products: list[AudibleProduct]

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [product.to_audiobook() for product in self.products]


class AudibleSimilarResponse(_Response):
    similar_products: list[AudibleProduct]

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [sim.to_audiobook() for sim in self.similar_products]


class AudibleSingleResponse(_Response):
    product: AudibleProduct

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [self.product.to_audiobook()]
