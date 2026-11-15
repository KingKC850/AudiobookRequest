"""Request stages, the library check, declining, and the wishlist page."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.main
from app.internal.audible.types import AudibleProduct, in_region_language
from app.internal.library.index import LibraryIndex, library_index
from app.internal.library.owned import refresh_library_index, sync_owned
from app.internal.matching import auto
from app.internal.matching.config import match_config
from app.internal.models import Audiobook, AudiobookRequest
from app.internal.retry import job
from app.internal.retry.pool import StageRow, get_rows
from app.internal.retry.state import GrabInfo, retry_config
from tests.constants import ASIN, FULFILLED_ASIN
from tests.http import as_dict, json_list
from tests.mock_services import MockServer

UNTRUSTED_ASIN = "B00UNTRUSTED"


@pytest.fixture(autouse=True)
def setup(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(auto, "FALLBACK_QUERY_DELAY_SECONDS", 0)
    match_config.set_strict(db, True)
    retry_config.set_delay_seconds(db, 0)
    library_index.clear()
    db.add(
        Audiobook(
            asin=UNTRUSTED_ASIN,
            title="Untrusted Book: A Subtitle",
            subtitle=None,
            authors=["Jane Q. Writer"],
            narrators=[],
            cover_image=None,
            release_date=datetime(2020, 1, 1, tzinfo=UTC),
            runtime_length_min=600,
        )
    )
    db.commit()
    db.add(AudiobookRequest(asin=UNTRUSTED_ASIN, user_username="untrusted"))
    db.commit()
    yield
    library_index.clear()


@pytest.fixture
def untrusted_client() -> Iterator[TestClient]:
    token = base64.b64encode(b"untrusted:untrustedpass").decode()
    with TestClient(
        app.main.app,
        headers={"Authorization": f"Basic {token}"},
        raise_server_exceptions=False,
    ) as client:
        yield client


def _stages(db: Session, username: str | None = None) -> dict[str, StageRow]:
    return {r.result.book.asin: r for r in get_rows(db, username)}


def _grab(db: Session, asin: str, when: datetime | None = None) -> None:
    book = db.get(Audiobook, asin)
    assert book is not None
    book.fulfilled = True
    db.add(book)
    db.commit()
    retry_config.record_attempt(
        db,
        asin,
        "title",
        "grabbed: release",
        GrabInfo(release="release", indexer="idx", when=when or datetime.now(UTC)),
    )


def _refresh(db: Session) -> None:
    async def run() -> None:
        async with ClientSession() as client_session:
            assert await refresh_library_index(db, client_session)

    asyncio.run(run())


# -- stages ----------------------------------------------------------------
def test_stage_follows_who_asked_and_what_happened(db: Session) -> None:
    rows = _stages(db)
    assert rows[UNTRUSTED_ASIN].stage == "pending"
    assert rows[ASIN].stage == "searching"
    # fulfilled before the fork kept any history, and nothing to check against
    assert rows[FULFILLED_ASIN].stage == "library"

    # a user only sees their own requests
    assert set(_stages(db, "untrusted")) == {UNTRUSTED_ASIN}

    # without Audiobookshelf there is no way to tell, so a grab counts as done
    _grab(db, ASIN)
    assert _stages(db)[ASIN].stage == "library"


def test_grab_is_downloading_until_the_library_has_it(
    db: Session, abs_service: MockServer
) -> None:
    _grab(db, ASIN, datetime.now(UTC) - timedelta(days=5))
    row = _stages(db)[ASIN]
    assert row.stage == "downloading"
    assert row.stuck

    retry_config.set_stuck_days(db, 10)
    assert not _stages(db)[ASIN].stuck

    # the mock library holds a book with this ASIN
    _refresh(db)
    assert sync_owned(db) == 1
    row = _stages(db)[ASIN]
    assert row.stage == "library"
    assert row.record is not None and row.record.in_library_at is not None
    _ = abs_service


def test_library_index_marks_owned_books(db: Session, abs_service: MockServer) -> None:
    book = db.get(Audiobook, ASIN)
    assert book is not None and not book.fulfilled and not book.in_library

    _refresh(db)
    assert book.in_library
    _ = sync_owned(db)
    db.expire_all()
    book = db.get(Audiobook, ASIN)
    assert book is not None and book.fulfilled
    _ = abs_service


def test_library_index_matching() -> None:
    index = LibraryIndex()
    assert not index.owns("B001", "Dune", ["Frank Herbert"])  # not loaded yet
    index.load(
        [
            ("b001", "Something", "Somebody"),
            (None, "Dune", "Frank Herbert"),
            (None, "The Way of Kings", "Brandon Sanderson, Another Person"),
        ]
    )
    assert index.owns("B001", "Whatever", [])
    # no Audible id on the item: title without subtitle, plus an author surname
    assert index.owns("B999", "Dune: Deluxe Edition", ["Frank Herbert"])
    index.load([(None, "Mistborn: The Final Empire", "Brandon Sanderson")])
    assert index.owns("B1", "Mistborn: The Final Empire", ["Brandon Sanderson"])
    assert not index.owns(
        "B2", "Mistborn: The Well of Ascension", ["Brandon Sanderson"]
    )
    index.load(
        [
            (None, "Dune", "Frank Herbert"),
            (None, "The Way of Kings", "Brandon Sanderson, Another Person"),
        ]
    )
    assert index.owns("B999", "The Way of Kings", ["Brandon Sanderson"])
    assert not index.owns("B999", "Dune", ["Somebody Else"])
    assert not index.owns("B999", "Dune Messiah", ["Frank Herbert"])


def test_search_language_filter() -> None:
    def product(asin: str, language: str | None) -> AudibleProduct:
        return AudibleProduct(
            asin=asin, release_date="2024-01-01", title="t", language=language
        )

    products = [product("en", "english"), product("de", "german"), product("x", None)]
    assert [p.asin for p in in_region_language(products, "us")] == ["en", "x"]
    assert [p.asin for p in in_region_language(products, "de")] == ["de", "x"]


# -- admin actions ---------------------------------------------------------
def test_decline_and_restore(
    admin_client: TestClient, db: Session, prowlarr: MockServer
) -> None:
    response = admin_client.post(
        f"/api/retry/decline/{ASIN}", json={"note": "Abridged only"}
    )
    assert response.status_code == 204
    retry_config.flush()
    assert _stages(db)[ASIN].stage == "declined"
    db.expire_all()
    book = db.get(Audiobook, ASIN)
    assert book is not None and book.admin_note == "Abridged only"

    # a declined book is never searched
    asyncio.run(job.run_retry(force=True))
    assert prowlarr.requests_to("/prowlarr/api/v1/search") == []

    assert admin_client.delete(f"/api/retry/decline/{ASIN}").status_code == 204
    retry_config.flush()
    assert _stages(db)[ASIN].stage == "searching"
    assert admin_client.post("/api/retry/decline/NOPE", json={}).status_code == 404


def test_requeue_and_mark_in_library(
    admin_client: TestClient, db: Session, abs_service: MockServer
) -> None:
    _grab(db, UNTRUSTED_ASIN)
    assert _stages(db)[UNTRUSTED_ASIN].stage == "downloading"

    assert admin_client.post(f"/api/retry/requeue/{UNTRUSTED_ASIN}").status_code == 204
    retry_config.flush()
    db.expire_all()
    row = _stages(db)[UNTRUSTED_ASIN]
    assert row.stage == "searching" and row.approved_only
    assert row.record is not None and row.record.grabbed is None

    _grab(db, UNTRUSTED_ASIN)
    assert admin_client.post(f"/api/retry/library/{UNTRUSTED_ASIN}").status_code == 204
    retry_config.flush()
    assert _stages(db)[UNTRUSTED_ASIN].stage == "library"
    _ = abs_service


def test_release_picked_by_hand_is_recorded(
    admin_client: TestClient, db: Session, prowlarr: MockServer
) -> None:
    assert admin_client.post(f"/api/requests/{ASIN}/refresh").status_code == 202
    response = admin_client.post(
        f"/api/requests/{ASIN}/download",
        json={"guid": "mock-guid-1", "indexer_id": 1},
    )
    assert response.status_code == 204
    retry_config.flush()
    record = retry_config.get_state(db)[ASIN]
    assert record.grabbed is not None
    assert record.grabbed.release == "Test Book m4b"
    assert record.grabbed.indexer == "MockIndexer"
    _ = prowlarr


def test_stage_api_is_scoped(
    admin_client: TestClient, untrusted_client: TestClient
) -> None:
    assert len(json_list(admin_client.get("/api/retry/stages"))) == 3
    mine = json_list(untrusted_client.get("/api/retry/stages"))
    assert [as_dict(row)["stage"] for row in mine] == ["pending"]
    assert untrusted_client.post(f"/api/retry/library/{ASIN}").status_code == 403
    assert (
        untrusted_client.post(f"/api/retry/decline/{ASIN}", json={}).status_code == 403
    )


# -- pages -----------------------------------------------------------------
def test_admin_wishlist_page(admin_client: TestClient) -> None:
    page = admin_client.get("/wishlist")
    assert page.status_code == 200
    # lands on what needs an admin
    assert "Untrusted Book: A Subtitle" in page.text
    assert "Test Book" not in page.text.replace("Untrusted Book", "")
    for label in ("Needs approval", "Searching", "Downloading", "In library", "Manual"):
        assert label in page.text
    assert "hx-approve/B00UNTRUSTED" in page.text
    assert "Decline" in page.text
    # sorting and the submitter filter
    assert "Submitted by" in page.text
    assert '<option value="untrusted">' in page.text
    assert "Date submitted" in page.text
    assert 'data-submitters="untrusted"' in page.text
    assert 'data-default-sort="requested"' in page.text

    searching = admin_client.get("/wishlist?stage=searching")
    assert "Test Book" in searching.text
    assert "Untrusted Book" not in searching.text
    assert "Search all now" in searching.text
    assert "/wishlist/sources/" in searching.text

    everything = admin_client.get("/wishlist?stage=all")
    for title in ("Untrusted Book", "Test Book", "Fulfilled Book"):
        assert title in everything.text

    badge = admin_client.get("/wishlist/stages/hx-badge")
    assert badge.status_code == 200
    assert "Needs approval" in badge.text and "stage=pending" in badge.text


def test_user_wishlist_page(untrusted_client: TestClient, db: Session) -> None:
    page = untrusted_client.get("/wishlist")
    assert page.status_code == 200
    assert "Untrusted Book" in page.text
    assert "Test Book" not in page.text.replace("Untrusted Book", "")
    assert "Waiting for approval" in page.text
    assert "Submitted by" not in page.text
    assert "hx-approve" not in page.text
    assert "submitted by" not in page.text
    assert "hx-badge" not in page.text

    assert untrusted_client.get("/wishlist/stages/hx-badge").status_code in (401, 403)
    response = untrusted_client.post(
        f"/wishlist/stages/hx-approve/{UNTRUSTED_ASIN}", headers={"HX-Request": "true"}
    )
    assert response.status_code in (200, 401, 403)
    retry_config.flush()
    assert retry_config.get_approved(db) == set()

    # a user can take their own request back
    response = untrusted_client.delete(
        f"/wishlist/stages/hx-request/{UNTRUSTED_ASIN}?stage=all",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Untrusted Book" not in response.text


def test_page_actions(admin_client: TestClient, db: Session) -> None:
    hx = {"HX-Request": "true"}
    response = admin_client.post(
        f"/wishlist/stages/hx-decline/{UNTRUSTED_ASIN}?stage=pending",
        data={"note": "Not this one"},
        headers=hx,
    )
    assert response.status_code == 200
    assert 'id="stage-content"' in response.text
    assert 'id="tablist"' in response.text  # counts are refreshed with it
    assert "Nothing is waiting on you" in response.text

    declined = admin_client.get("/wishlist?stage=declined")
    assert "Not this one" in declined.text and "Restore" in declined.text

    response = admin_client.delete(
        f"/wishlist/stages/hx-decline/{UNTRUSTED_ASIN}?stage=declined", headers=hx
    )
    assert response.status_code == 200
    assert "Untrusted Book" not in response.text

    response = admin_client.post(
        f"/wishlist/stages/hx-approve/{UNTRUSTED_ASIN}?stage=pending", headers=hx
    )
    assert response.status_code == 200
    retry_config.flush()
    assert retry_config.get_approved(db) == {UNTRUSTED_ASIN}

    response = admin_client.delete(
        f"/wishlist/stages/hx-approve/{UNTRUSTED_ASIN}?stage=searching", headers=hx
    )
    assert response.status_code == 200
    retry_config.flush()
    assert retry_config.get_approved(db) == set()


def test_top_bar_names_things_and_shows_status(
    admin_client: TestClient,
    untrusted_client: TestClient,
    db: Session,
    abs_service: MockServer,
) -> None:
    page = untrusted_client.get("/wishlist")
    assert "My requests" in page.text
    assert "hx-status" in page.text
    for label in ("Search", "Settings", "Theme", "Home"):
        assert label in page.text

    admin_page = admin_client.get("/wishlist")
    assert "My requests" not in admin_page.text
    assert "Requests" in admin_page.text

    # nothing to say yet: no pills, but a count of what is in progress on phones
    status = untrusted_client.get("/wishlist/stages/hx-status")
    assert status.status_code == 200
    assert "ready" not in status.text and "on the way" not in status.text
    compact = untrusted_client.get("/wishlist/stages/hx-status?compact=true")
    assert ">1<" in compact.text

    _grab(db, UNTRUSTED_ASIN)
    status = untrusted_client.get("/wishlist/stages/hx-status")
    assert "1 on the way" in status.text and "ready" not in status.text

    retry_config.mark_in_library(db, UNTRUSTED_ASIN)
    status = untrusted_client.get("/wishlist/stages/hx-status")
    assert "1 ready" in status.text and "on the way" not in status.text

    # after a week it is no longer news
    state = retry_config.get_state(db)
    state[UNTRUSTED_ASIN].in_library_at = datetime.now(UTC) - timedelta(days=8)
    retry_config.set_state(db, state)
    status = untrusted_client.get("/wishlist/stages/hx-status")
    assert "ready" not in status.text

    # an admin's pills count everyone's books
    _grab(db, ASIN)
    assert "1 on the way" in admin_client.get("/wishlist/stages/hx-status").text
    assert "on the way" not in untrusted_client.get("/wishlist/stages/hx-status").text
    _ = abs_service
