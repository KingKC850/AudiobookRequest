"""Decides which release, if any, an automatic download may grab."""

import asyncio

import pydantic
from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.matching.config import match_config
from app.internal.matching.strict import (
    Book,
    person,
    query_title,
    strict_matches,
    surnames,
)
from app.internal.models import ManualBookRequest, ProwlarrSource
from app.internal.prowlarr.prowlarr import query_prowlarr
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.download_ranking import rank_sources
from app.internal.ranking.quality import quality_config
from app.util.log import logger

# Pause between two consecutive Prowlarr queries for the same book.
FALLBACK_QUERY_DELAY_SECONDS = 2.0


class AutoPick(pydantic.BaseModel):
    source: ProwlarrSource | None = None
    strict: bool = False
    """Strict matching was applied. If so and `source` is None, `note` says why."""
    query: str | None = None
    """The fallback query that found the source. None if the normal search did."""
    note: str = ""


async def _search(
    session: Session,
    client_session: ClientSession,
    book: Book,
    query: str,
    force_refresh: bool,
) -> list[ProwlarrSource]:
    sources = await query_prowlarr(
        session,
        client_session,
        book,
        indexer_ids=prowlarr_config.get_indexers(session),
        force_refresh=force_refresh,
        query_override=query,
    )
    if not sources:
        return []
    return await rank_sources(
        session,
        client_session,
        sources,
        book,
        isinstance(book, ManualBookRequest),
    )


async def _fallback_search(
    session: Session,
    client_session: ClientSession,
    book: Book,
    force_refresh: bool,
) -> AutoPick:
    """Search Prowlarr again with shorter queries.

    The normal search uses the full Audible title, which often does not appear
    in release names. Results still have to pass the strict match.
    """
    title = query_title(book.title)
    if not title:
        return AutoPick(strict=True, note="no usable title for a fallback search")

    narrators = [person(n) for n in book.narrators if person(n)]
    authors = surnames(book.authors)
    min_seeders = quality_config.get_min_seeders(session)

    # a) title + narrator: the query itself filters by narrator
    if narrators:
        query = f"{title} {narrators[0]}"
        ranked = await _search(session, client_session, book, query, force_refresh)
        matches = strict_matches(
            book,
            ranked,
            require_narrator=match_config.get_require_narrator(session),
            narrator_in_query=True,
            min_seeders=min_seeders,
        )
        if matches:
            return AutoPick(source=matches[0], strict=True, query=query)
        await asyncio.sleep(FALLBACK_QUERY_DELAY_SECONDS)

    # b) title + author: the narrator cannot be verified from these results
    query = f"{title} {authors[0]}" if authors else title
    ranked = await _search(session, client_session, book, query, force_refresh)
    matches = strict_matches(
        book, ranked, require_narrator=False, min_seeders=min_seeders
    )
    if not matches:
        return AutoPick(
            strict=True,
            note=f"fallback search: {len(ranked)} results for '{query}', none matched",
        )
    if narrators and not match_config.get_allow_unverified_narrator(session):
        return AutoPick(
            strict=True,
            note=(
                f"fallback search found {len(matches)} title/author match(es) but "
                f"the narrator is unverified, e.g. '{matches[0].title}'. "
                "Review the sources by hand."
            ),
        )
    return AutoPick(source=matches[0], strict=True, query=query)


async def choose_auto_download(
    session: Session,
    client_session: ClientSession,
    book: Book,
    ranked: list[ProwlarrSource],
    force_refresh: bool = False,
) -> AutoPick:
    """Picks the release to download automatically for a book.

    `ranked` are the already ranked results of the normal search.
    """
    if not match_config.get_strict(session):
        # Previous behaviour: the best ranked source wins, whatever it is.
        return AutoPick(source=ranked[0] if ranked else None)

    matches = strict_matches(
        book,
        ranked,
        require_narrator=match_config.get_require_narrator(session),
        min_seeders=quality_config.get_min_seeders(session),
    )
    if matches:
        return AutoPick(source=matches[0], strict=True)

    note = f"{len(ranked)} results for the full title, none matched strictly"
    if not match_config.get_fallback_search(session):
        return AutoPick(strict=True, note=note)

    await asyncio.sleep(FALLBACK_QUERY_DELAY_SECONDS)
    fallback = await _fallback_search(session, client_session, book, force_refresh)
    if fallback.source is None:
        fallback.note = f"{note}; {fallback.note}"
    logger.info(
        "Fallback search finished",
        title=book.title,
        found=fallback.source is not None,
        query=fallback.query,
        note=fallback.note,
    )
    return fallback
