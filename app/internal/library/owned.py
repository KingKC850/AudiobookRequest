"""A local picture of what is in the Audiobookshelf library.

The whole library is listed once in a while and kept in memory, so "do we
already have this book?" is a dictionary lookup instead of a search request
per book. Used to show owned books as owned everywhere, and to notice when a
download has actually arrived.
"""

import posixpath

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, not_, select

from app.internal.audiobookshelf.config import abs_config
from app.internal.library.index import library_index
from app.internal.models import Audiobook
from app.internal.retry.state import retry_config
from app.util.connection import USER_AGENT
from app.util.db import get_session
from app.util.log import logger


class _Metadata(BaseModel):
    title: str | None = None
    authorName: str | None = None
    asin: str | None = None


class _Media(BaseModel):
    metadata: _Metadata = _Metadata()


class _Item(BaseModel):
    media: _Media = _Media()


class _Items(BaseModel):
    results: list[_Item] = []


def library_check_available(session: Session) -> bool:
    """True if arrivals can be confirmed against Audiobookshelf."""
    return abs_config.is_valid(session) and abs_config.get_check_downloaded(session)


async def refresh_library_index(
    session: Session, client_session: ClientSession
) -> bool:
    """Lists the whole Audiobookshelf library. Returns False if that failed."""
    if not library_check_available(session):
        library_index.clear()
        return False
    base_url = abs_config.get_base_url(session)
    lib_id = abs_config.get_library_id(session)
    token = abs_config.get_api_token(session)
    if not base_url or not lib_id or not token:
        return False
    url = posixpath.join(base_url, f"api/libraries/{lib_id}/items")
    try:
        async with client_session.get(
            url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            params={"limit": "0", "minified": "1"},
        ) as response:
            if not response.ok:
                logger.warning(
                    "Library index: Audiobookshelf refused the listing",
                    status=response.status,
                )
                return False
            payload = _Items.model_validate(await response.json())
    except Exception as e:
        logger.warning("Library index: could not list Audiobookshelf", error=str(e))
        return False

    library_index.load(
        [
            (i.media.metadata.asin, i.media.metadata.title, i.media.metadata.authorName)
            for i in payload.results
        ]
    )
    logger.info("Library index refreshed", books=len(payload.results))
    return True


def sync_owned(session: Session) -> int:
    """Marks every known book that is in the library as fulfilled, and records
    the arrival of the ones that were downloading. Returns how many arrived."""
    if not library_index.loaded:
        return 0

    marked = 0
    for book in session.exec(select(Audiobook).where(not_(Audiobook.fulfilled))).all():
        if library_index.owns(book.asin, book.title, book.authors):
            book.fulfilled = True
            session.add(book)
            marked += 1
    if marked:
        session.commit()
        logger.info("Library index: marked owned books as fulfilled", count=marked)

    # downloads that have now arrived
    waiting = [
        key
        for key, record in retry_config.get_state(session).items()
        if record.grabbed and not record.in_library_at
    ]
    arrived: list[str] = []
    if waiting:
        books = session.exec(
            select(Audiobook).where(col(Audiobook.asin).in_(waiting))
        ).all()
        arrived = [
            b.asin
            for b in books
            if b.fulfilled and library_index.owns(b.asin, b.title, b.authors)
        ]
        if arrived:
            retry_config.mark_in_library(session, *arrived)
            logger.info("Library index: downloads arrived", asins=arrived)
    return len(arrived)


async def scheduled_library_check() -> None:
    """Called by the scheduler: refresh the index and update what has arrived."""
    try:
        with next(get_session()) as session:
            async with ClientSession() as client_session:
                if await refresh_library_index(session, client_session):
                    _ = sync_owned(session)
    except Exception as e:
        logger.error("Library index: scheduled check failed", error=str(e))
