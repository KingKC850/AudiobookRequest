"""Retry loop, approvals and the history of automatic downloads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.matching import auto
from app.internal.matching.config import match_config
from app.internal.models import Audiobook, AudiobookRequest
from app.internal.retry import job
from app.internal.retry.pool import get_overview
from app.internal.retry.state import GrabInfo, RetryRecord, retry_config
from tests.constants import ASIN
from tests.http import as_list, json_object
from tests.mock_services import MockServer

UNTRUSTED_ASIN = "B00UNTRUSTED"


@pytest.fixture(autouse=True)
def fast_and_strict(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "FALLBACK_QUERY_DELAY_SECONDS", 0)
    match_config.set_strict(db, True)
    retry_config.set_delay_seconds(db, 0)
    # a request that only an untrusted user made
    db.add(
        Audiobook(
            asin=UNTRUSTED_ASIN,
            title="Untrusted Book",
            subtitle=None,
            authors=["Mock Author"],
            narrators=[],
            cover_image=None,
            release_date=datetime(2020, 1, 1, tzinfo=UTC),
            runtime_length_min=600,
        )
    )
    db.commit()
    db.add(AudiobookRequest(asin=UNTRUSTED_ASIN, user_username="untrusted"))
    db.commit()


def _strict_release(prowlarr: MockServer, title: str = "Test Book") -> None:
    release = dict(prowlarr.state["search_results"][0])
    release["guid"] = "mock-guid-strict"
    release["title"] = f"Mock Author - {title} [m4b]"
    prowlarr.state["search_results"] = [release]


def _grabs(prowlarr: MockServer) -> list[bytes]:
    return [
        r["body"]
        for r in prowlarr.requests_to("/prowlarr/api/v1/search")
        if r["method"] == "POST"
    ]


def _searches(prowlarr: MockServer) -> list[str]:
    return [
        r["query"]["query"]
        for r in prowlarr.requests_to("/prowlarr/api/v1/search")
        if r["method"] == "GET"
    ]


# -- pool ------------------------------------------------------------------
def test_pool_splits_by_who_asked(db: Session) -> None:
    overview = get_overview(db)
    assert [r.book.asin for r in overview.pending] == [UNTRUSTED_ASIN]
    assert [i.result.book.asin for i in overview.hunting] == [ASIN]
    assert not overview.hunting[0].approved_only

    retry_config.approve(db, UNTRUSTED_ASIN)
    overview = get_overview(db)
    assert overview.pending == []
    by_asin = {i.result.book.asin: i for i in overview.hunting}
    assert by_asin[UNTRUSTED_ASIN].approved_only

    retry_config.unapprove(db, UNTRUSTED_ASIN)
    assert [r.book.asin for r in get_overview(db).pending] == [UNTRUSTED_ASIN]


# -- retry job -------------------------------------------------------------
def test_run_grabs_strict_match_and_never_touches_unapproved(
    db: Session, prowlarr: MockServer
) -> None:
    _strict_release(prowlarr)
    asyncio.run(job.run_retry())

    assert len(_grabs(prowlarr)) == 1
    assert all("Untrusted" not in q for q in _searches(prowlarr))

    db.expire_all()
    book = db.get(Audiobook, ASIN)
    assert book is not None and book.fulfilled
    record = retry_config.get_state(db)[ASIN]
    assert record.tries == 1
    assert record.grabbed is not None
    assert record.grabbed.release == "Mock Author - Test Book [m4b]"
    assert record.grabbed.indexer == "MockIndexer"
    assert UNTRUSTED_ASIN not in retry_config.get_state(db)


def test_run_records_a_miss_and_waits_before_trying_again(
    db: Session, prowlarr: MockServer
) -> None:
    # the default mock release has no author in its name: never a strict match
    asyncio.run(job.run_retry())
    record = retry_config.get_state(db)[ASIN]
    assert record.tries == 1
    assert record.grabbed is None
    assert record.last_result is not None and "no match" in record.last_result
    assert _grabs(prowlarr) == []

    searches = len(_searches(prowlarr))
    asyncio.run(job.run_retry())
    assert len(_searches(prowlarr)) == searches, "tried again too soon"
    assert retry_config.get_state(db)[ASIN].tries == 1

    # forced, or once enough time has passed, it is tried again
    asyncio.run(job.run_retry(force=True))
    assert retry_config.get_state(db)[ASIN].tries == 2

    state = retry_config.get_state(db)
    state[ASIN].last_tried = datetime.now(UTC) - timedelta(hours=21)
    retry_config.set_state(db, state)
    asyncio.run(job.run_retry())
    assert retry_config.get_state(db)[ASIN].tries == 3


def test_run_is_skipped_without_strict_matching(
    db: Session, prowlarr: MockServer
) -> None:
    match_config.set_strict(db, False)
    asyncio.run(job.run_retry(force=True))
    assert _searches(prowlarr) == []
    assert _grabs(prowlarr) == []


def test_scheduled_run_needs_the_setting(db: Session, prowlarr: MockServer) -> None:
    asyncio.run(job.scheduled_retry())
    assert _searches(prowlarr) == []

    retry_config.set_enabled(db, True)
    asyncio.run(job.scheduled_retry())
    assert _searches(prowlarr) != []


def test_max_per_run_rotates_through_the_pool(
    db: Session, prowlarr: MockServer
) -> None:
    retry_config.approve(db, UNTRUSTED_ASIN)
    retry_config.set_max_per_run(db, 1)

    asyncio.run(job.run_retry(force=True))
    first = set(retry_config.get_state(db))
    assert len(first) == 1
    asyncio.run(job.run_retry(force=True))
    assert set(retry_config.get_state(db)) == {ASIN, UNTRUSTED_ASIN}
    _ = prowlarr


def test_prune_keeps_grabs_and_drops_stale_approvals(db: Session) -> None:
    now = datetime.now(UTC)
    retry_config.set_state(
        db,
        {
            "GONE-MISS": RetryRecord(first_seen=now, tries=3, last_result="no match"),
            "GONE-GRAB": RetryRecord(
                first_seen=now,
                tries=1,
                grabbed=GrabInfo(release="r", indexer="i", when=now),
            ),
            ASIN: RetryRecord(first_seen=now, tries=1),
        },
    )
    retry_config.approve(db, "GONE-MISS", ASIN)
    retry_config.prune(db, {ASIN})
    assert set(retry_config.get_state(db)) == {"GONE-GRAB", ASIN}
    assert retry_config.get_approved(db) == {ASIN}


# -- API and pages ---------------------------------------------------------
def test_approve_searches_right_away(
    admin_client: TestClient, db: Session, prowlarr: MockServer
) -> None:
    _strict_release(prowlarr, "Untrusted Book")
    response = admin_client.post(f"/api/retry/approve/{UNTRUSTED_ASIN}")
    assert response.status_code == 204
    assert len(_grabs(prowlarr)) == 1

    db.expire_all()
    book = db.get(Audiobook, UNTRUSTED_ASIN)
    assert book is not None and book.fulfilled

    status = json_object(admin_client.get("/api/retry"))
    assert status["pending"] == []
    assert len(as_list(status["grabbed"])) == 1


def test_unapprove_and_unknown_book(admin_client: TestClient, db: Session) -> None:
    assert admin_client.post("/api/retry/approve/NOPE").status_code == 404
    retry_config.approve(db, UNTRUSTED_ASIN)
    response = admin_client.delete(f"/api/retry/approve/{UNTRUSTED_ASIN}")
    assert response.status_code == 204
    retry_config.flush()
    assert retry_config.get_approved(db) == set()


def test_approve_all_and_run_now(
    admin_client: TestClient, db: Session, prowlarr: MockServer
) -> None:
    assert admin_client.post("/api/retry/approve-all").status_code == 204
    retry_config.flush()
    assert retry_config.get_approved(db) == {UNTRUSTED_ASIN}
    assert any("Untrusted" in q for q in _searches(prowlarr))

    before = len(_searches(prowlarr))
    assert admin_client.post("/api/retry/run").status_code == 202
    assert len(_searches(prowlarr)) > before


def test_retry_api_is_admin_only(trusted_client: TestClient) -> None:
    assert trusted_client.get("/api/retry").status_code == 403
    assert trusted_client.post("/api/retry/run").status_code == 403
    assert trusted_client.post(f"/api/retry/approve/{ASIN}").status_code == 403
    assert trusted_client.get("/api/settings/retry").status_code == 403


def test_retry_settings_api_and_page(admin_client: TestClient) -> None:
    assert admin_client.get("/api/settings/retry").json() == {
        "enabled": False,
        "min_hours": 20,
        "delay_seconds": 0,
        "max_per_run": 0,
        "stuck_days": 3,
    }
    response = admin_client.post(
        "/settings/retry/hx-form",
        data={
            "enabled": "on",
            "min_hours": "12",
            "delay_seconds": "30",
            "max_per_run": "5",
            "stuck_days": "7",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'id="retry-settings"' in response.text
    assert admin_client.get("/api/settings/retry").json() == {
        "enabled": True,
        "min_hours": 12,
        "delay_seconds": 30,
        "max_per_run": 5,
        "stuck_days": 7,
    }
    assert admin_client.get("/settings/retry/hx-form").status_code == 200

    response = admin_client.patch(
        "/api/settings/retry",
        json={"enabled": True, "min_hours": 0, "delay_seconds": 0, "max_per_run": 0},
    )
    assert response.status_code == 422
