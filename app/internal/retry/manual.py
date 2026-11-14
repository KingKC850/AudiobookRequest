"""Remembers a release an admin picked by hand, so it shows as downloading."""

from datetime import UTC, datetime

from sqlmodel import Session

from app.internal.models import Audiobook
from app.internal.prowlarr.util import prowlarr_config, prowlarr_source_cache
from app.internal.retry.state import GrabInfo, retry_config


def record_manual_grab(session: Session, key: str, guid: str, indexer_id: int):
    ttl = prowlarr_config.get_source_ttl(session)
    source = next(
        (
            s
            for sources in prowlarr_source_cache.get_all(ttl).values()
            for s in sources
            if s.guid == guid and s.indexer_id == indexer_id
        ),
        None,
    )
    book = session.get(Audiobook, key)
    release = source.title if source else "picked by hand"
    retry_config.record_attempt(
        session,
        key,
        book.title if book else None,
        f"picked by hand: {release}",
        GrabInfo(
            release=release,
            indexer=source.indexer if source else f"indexer {indexer_id}",
            info_url=source.info_url if source else None,
            when=datetime.now(UTC),
        ),
    )
