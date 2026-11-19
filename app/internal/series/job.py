"""The series watch list: works out what is missing and asks for it."""

import asyncio
import secrets
from datetime import UTC, datetime

import aiohttp
from aiohttp import ClientSession
from sqlmodel import Session, col, select

from app.internal.audible.search import upsert_books
from app.internal.audible.types import audible_region_type, audible_regions
from app.internal.auth.authentication import create_user
from app.internal.library.index import library_index
from app.internal.library.owned import library_check_available, refresh_library_index
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    EventEnum,
    GroupEnum,
    User,
)
from app.internal.notifications import send_all_notifications
from app.internal.query import background_start_query
from app.internal.ranking.quality import quality_config
from app.internal.retry.state import retry_config
from app.internal.series.audible import SeriesBook, SeriesInfo, get_series
from app.internal.series.config import (
    RequestedBook,
    SeriesMode,
    WatchedSeries,
    series_config,
)
from app.internal.series.plan import SeriesPlan, build_plan
from app.util.db import get_session
from app.util.log import logger

_running = False


def is_running() -> bool:
    return _running


def _region(region: str) -> audible_region_type:
    if region in audible_regions:
        return region
    return "us"


def ensure_series_user(session: Session) -> str:
    """The account series requests are made under. Created on first use, as a
    trusted user nobody can sign in as."""
    username = series_config.get_user(session)
    user = session.get(User, username)
    if user is None:
        session.add(create_user(username, secrets.token_urlsafe(32), GroupEnum.trusted))
        session.commit()
        logger.info("Series: created the account for series requests", user=username)
    elif not user.is_above(GroupEnum.trusted):
        logger.warning(
            "Series: the account for series requests is not trusted, "
            + "so its requests will wait for approval",
            user=username,
        )
    return username


def plan_for(
    session: Session,
    info: SeriesInfo,
    mode: SeriesMode,
    watch_since: datetime,
) -> SeriesPlan:
    """What the watch list would do with this series right now."""
    asins = [b.asin for b in info.books]
    fulfilled = set(
        session.exec(
            select(Audiobook.asin).where(
                col(Audiobook.asin).in_(asins), col(Audiobook.fulfilled)
            )
        ).all()
    )
    asked = set(
        session.exec(
            select(AudiobookRequest.asin).where(col(AudiobookRequest.asin).in_(asins))
        ).all()
    )

    def in_library(book: SeriesBook) -> bool:
        return book.asin in fulfilled or library_index.owns(
            book.asin, book.title, book.authors
        )

    def requested(book: SeriesBook) -> bool:
        return book.asin in asked

    return build_plan(
        info.books,
        mode,
        in_library,
        requested,
        watch_since,
        language_region=(
            info.region if series_config.get_language_filter(session) else None
        ),
    )


def request_books(
    session: Session, books: list[SeriesBook], username: str
) -> list[SeriesBook]:
    """Adds a request for each book under the given account. Returns the books
    a request was actually added for."""
    if not books:
        return []
    _ = upsert_books(session, [b.to_audiobook() for b in books])
    existing = set(
        session.exec(
            select(AudiobookRequest.asin).where(
                col(AudiobookRequest.asin).in_([b.asin for b in books]),
                AudiobookRequest.user_username == username,
            )
        ).all()
    )
    added = [b for b in books if b.asin not in existing]
    for book in added:
        session.add(AudiobookRequest(asin=book.asin, user_username=username))
    session.commit()
    return added


async def start_downloads(asins: list[str]) -> None:
    """Announces the new requests and searches for each, one after the other."""
    if not asins:
        return
    with next(get_session()) as session:
        auto = quality_config.get_auto_download(session)
        delay = retry_config.get_delay_seconds(session)
    for index, asin in enumerate(asins):
        try:
            await send_all_notifications(
                event_type=EventEnum.on_new_request, book_asin=asin
            )
        except Exception as e:
            logger.error("Series: notification failed", asin=asin, error=str(e))
        if auto:
            if index:
                await asyncio.sleep(delay)
            await background_start_query(asin, auto_download=True)


async def _library_ready(session: Session, client_session: ClientSession) -> bool:
    """False if the library should be checked but could not be listed. Asking
    for books without knowing what is owned would request duplicates."""
    if not library_check_available(session):
        return True
    if await refresh_library_index(session, client_session):
        return True
    return library_index.loaded


async def check_series(
    session: Session,
    client_session: ClientSession,
    watched: WatchedSeries,
    limit: int | None = None,
) -> list[SeriesBook]:
    """Lists one watched series again and asks for what is missing. Returns the
    books that were requested."""
    now = datetime.now(UTC)
    info = await get_series(
        client_session,
        watched.asin,
        _region(watched.region),
        title_hint=watched.title,
        force=True,
    )
    if info is None:
        watched.last_checked = now
        watched.last_result = "could not list the series on Audible"
        series_config.put_series(session, watched)
        return []

    plan = plan_for(session, info, watched.mode, watched.added_at)
    wanted = plan.request
    held_back = 0
    if limit and len(wanted) > limit:
        held_back = len(wanted) - limit
        wanted = wanted[:limit]

    added = request_books(session, wanted, ensure_series_user(session))

    watched.title = info.title
    watched.cover_image = info.cover_image or watched.cover_image
    watched.last_checked = now
    watched.upcoming = plan.upcoming_text
    watched.requested.extend(
        RequestedBook(asin=b.asin, title=b.title, sequence=b.sequence, when=now)
        for b in added
    )
    if added:
        result = f"requested {len(added)} book{'s' if len(added) != 1 else ''}"
        if held_back:
            result += f", {held_back} more next time"
    else:
        result = "nothing new"
    watched.last_result = result
    series_config.put_series(session, watched)
    return added


async def run_series_check(only: str | None = None) -> None:
    """One pass over the confirmed series of the watch list."""
    global _running
    if _running:
        logger.info("Series: a check is already in progress, skipping")
        return
    _running = True
    try:
        await _run(only)
    except Exception as e:
        logger.error("Series: check failed", error=str(e))
    finally:
        _running = False


async def _run(only: str | None) -> None:
    requested: list[str] = []
    with next(get_session()) as session:
        watch = series_config.get_watch(session)
        todo = [
            s
            for s in watch.values()
            if s.confirmed and (only is None or s.asin == only)
        ]
        if only is None:
            series_config.set_last_run(session, datetime.now(UTC))
        if not todo:
            return
        limit = series_config.get_max_per_run(session) if only is None else None
        async with ClientSession(timeout=aiohttp.ClientTimeout(60)) as client_session:
            if not await _library_ready(session, client_session):
                logger.warning(
                    "Series: skipped, the Audiobookshelf library could not be listed"
                )
                return
            logger.info("Series: check started", series=len(todo))
            for index, watched in enumerate(todo):
                if index:
                    await asyncio.sleep(1)
                try:
                    added = await check_series(session, client_session, watched, limit)
                    requested.extend(b.asin for b in added)
                except Exception as e:
                    session.rollback()
                    logger.error(
                        "Series: check of one series failed",
                        series=watched.asin,
                        error=str(e),
                    )
    logger.info("Series: check finished", series=len(todo), requested=len(requested))
    await start_downloads(requested)


async def scheduled_series_check() -> None:
    """Called by the scheduler every so often. Runs the check once a day, from
    the configured hour on."""
    now = datetime.now()
    with next(get_session()) as session:
        if not series_config.get_enabled(session):
            return
        # at or after the hour, so a check missed while the app was down still runs
        if now.hour < series_config.get_run_hour(session):
            return
        last = series_config.get_last_run(session)
        if last and last.astimezone().date() == now.astimezone().date():
            return
    await run_series_check()
