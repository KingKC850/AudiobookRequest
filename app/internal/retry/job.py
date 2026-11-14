"""Searches again for open requests that have not been found yet."""

import asyncio
from datetime import UTC, datetime

import aiohttp
from aiohttp import ClientSession
from fastapi import HTTPException

from app.internal.download_backend import get_download_backend
from app.internal.matching.config import match_config
from app.internal.prowlarr.util import prowlarr_config
from app.internal.query import query_sources
from app.internal.retry.pool import HuntingItem, get_overview, open_keys
from app.internal.retry.state import retry_config
from app.util.db import get_session
from app.util.log import logger

_running = False


def is_running() -> bool:
    return _running


def _hours_since(when: datetime | None) -> float:
    if when is None:
        return float("inf")
    return (datetime.now(UTC) - when).total_seconds() / 3600


def _last_tried(item: HuntingItem) -> datetime:
    if item.record and item.record.last_tried:
        return item.record.last_tried
    return datetime.min.replace(tzinfo=UTC)


async def run_retry(force: bool = False) -> None:
    """One pass over the pool. `force` ignores the minimum time between tries."""
    global _running
    if _running:
        logger.info("Retry: a run is already in progress, skipping")
        return
    _running = True
    try:
        await _run(force)
    except Exception as e:
        logger.error("Retry: run failed", error=str(e))
    finally:
        _running = False


async def _run(force: bool) -> None:
    with next(get_session()) as session:
        if get_download_backend(session) != "prowlarr":
            logger.info("Retry: skipped, the download backend is not Prowlarr")
            return
        if not prowlarr_config.is_valid(session):
            logger.info("Retry: skipped, Prowlarr is not configured")
            return
        if not match_config.get_strict(session):
            # Without strict matching an unattended retry would grab loose matches.
            logger.warning("Retry: skipped, strict matching is turned off")
            return

        overview = get_overview(session)
        retry_config.prune(session, open_keys(session))

        min_hours = retry_config.get_min_hours(session)
        due = [
            item
            for item in overview.hunting
            if force or _hours_since(_last_tried(item)) >= min_hours
        ]
        # least recently tried first, so a limited run rotates through the pool
        due.sort(key=_last_tried)
        limit = retry_config.get_max_per_run(session)
        if limit:
            due = due[:limit]
        # read everything needed up front: commits below expire the ORM objects
        books = [(item.result.book.asin, item.result.book.title) for item in due]
        delay = retry_config.get_delay_seconds(session)

        logger.info("Retry: run started", due=len(books), pool=len(overview.hunting))
        grabbed = 0
        async with ClientSession(timeout=aiohttp.ClientTimeout(120)) as client_session:
            for index, (asin, title) in enumerate(books):
                if index:
                    await asyncio.sleep(delay)
                try:
                    _ = await query_sources(
                        asin,
                        session,
                        client_session,
                        force_refresh=True,
                        start_auto_download=True,
                    )
                    grabbed += 1
                except HTTPException as e:
                    # 404 (no match) and 500 (grab failed) are already recorded
                    if e.status_code not in (404, 500):
                        retry_config.record_attempt(
                            session, asin, title, f"error: {e.detail}"
                        )
                except Exception as e:
                    session.rollback()
                    retry_config.record_attempt(session, asin, title, f"error: {e}")
                    logger.error("Retry: search failed", asin=asin, error=str(e))
        logger.info("Retry: run finished", tried=len(books), grabbed=grabbed)


async def scheduled_retry() -> None:
    """Called by the scheduler. Does nothing unless retries are turned on."""
    with next(get_session()) as session:
        enabled = retry_config.get_enabled(session)
    if enabled:
        await run_retry()
