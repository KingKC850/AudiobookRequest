"""Strict matching and fallback search for automatic downloads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.matching import auto
from app.internal.matching.config import match_config
from app.internal.matching.strict import (
    Book,
    core_title,
    query_title,
    strict_matches,
    surnames,
)
from app.internal.models import (
    Audiobook,
    BookMetadata,
    ProwlarrSource,
    TorrentSource,
    UsenetSource,
)
from tests.constants import ASIN
from tests.mock_services import MockServer


def _book(
    title: str,
    authors: list[str],
    narrators: list[str] | None = None,
) -> Audiobook:
    return Audiobook(
        asin="B00STRICT",
        title=title,
        subtitle=None,
        authors=authors,
        narrators=narrators or [],
        cover_image=None,
        release_date=datetime(2020, 1, 1, tzinfo=UTC),
        runtime_length_min=600,
    )


def _torrent(
    title: str,
    seeders: int = 10,
    narrators: list[str] | None = None,
    authors: list[str] | None = None,
) -> TorrentSource:
    return TorrentSource(
        guid=title,
        indexer_id=1,
        indexer="MockIndexer",
        title=title,
        size=100_000_000,
        publish_date=datetime(2024, 1, 1, tzinfo=UTC),
        info_url=None,
        indexer_flags=[],
        seeders=seeders,
        leechers=0,
        book_metadata=BookMetadata(authors=authors or [], narrators=narrators or []),
    )


def _titles(sources: list[ProwlarrSource]) -> list[str]:
    return [s.title for s in sources]


# -- pure matching ---------------------------------------------------------
def test_short_title_needs_the_author_too() -> None:
    """A request for "Day" by Elie Wiesel must not match authors named Day."""
    book = _book("Day", ["Elie Wiesel"])
    sources: list[ProwlarrSource] = [
        _torrent("Sylvia Day - Bared to You"),
        _torrent("The Longest Day by Cornelius Ryan"),
        _torrent("Elie Wiesel - Day [m4b]"),
    ]
    assert _titles(strict_matches(book, sources)) == ["Elie Wiesel - Day [m4b]"]


def test_title_must_match_as_a_whole_phrase() -> None:
    book = _book("The Way of Kings", ["Brandon Sanderson"])
    sources: list[ProwlarrSource] = [
        _torrent("Brandon Sanderson - Kings of the Way"),
        _torrent("Brandon Sanderson - The Way of Kings"),
    ]
    assert _titles(strict_matches(book, sources)) == [
        "Brandon Sanderson - The Way of Kings"
    ]


def test_subtitle_accents_and_censored_words() -> None:
    assert core_title("Yesteryear: A GMA Book Club Pick") == "Yesteryear"
    assert core_title("Harry Potter - Gesprochen von Rufus Beck") == "Harry Potter"
    assert surnames(["Rufus Beck - Sprecher", "J.K. Rowling"]) == ["beck", "rowling"]

    book = _book("Der Phönix: Roman", ["Jürgen Müller"])
    assert strict_matches(book, [_torrent("Der Phonix - Jurgen Muller")])

    book = _book("The Subtle Art of Not Giving a F--k", ["Mark Manson"])
    assert strict_matches(
        book, [_torrent("Mark Manson - The Subtle Art of Not Giving a Fuck")]
    )
    assert query_title(book.title) == "The Subtle Art of Not Giving a"
    assert query_title("My Friend's Book: A Novel") == "My Book"


def test_other_narrator_is_a_different_edition() -> None:
    book = _book("Dune", ["Frank Herbert"], ["Scott Brick"])
    other = _torrent("Frank Herbert - Dune", narrators=["Simon Vance"])
    unknown = _torrent("Frank Herbert - Dune (unabridged)")
    right = _torrent("Frank Herbert - Dune", narrators=["Scott Brick"])

    assert strict_matches(book, [other]) == []
    # unknown narrator is accepted unless a narrator is required
    assert strict_matches(book, [unknown]) == [unknown]
    assert strict_matches(book, [unknown], require_narrator=True) == []
    # when the query was filtered by narrator, unknown counts as that narrator
    assert strict_matches(
        book, [unknown], require_narrator=True, narrator_in_query=True
    ) == [unknown]
    # a confirmed narrator beats an unknown one, whatever the ranked order
    assert strict_matches(book, [unknown, right]) == [right, unknown]


def test_closest_edition_wins_and_ranked_order_breaks_ties() -> None:
    book = _book("Dune", ["Frank Herbert"], ["Scott Brick"])
    full_cast = _torrent(
        "Frank Herbert - Dune",
        narrators=["Scott Brick", "Simon Vance", "Orlagh Cassidy"],
    )
    wordy = _torrent(
        "Dune Deluxe Anniversary Edition by Frank Herbert", narrators=["Scott Brick"]
    )
    plain_a = _torrent("Dune by Frank Herbert", narrators=["Scott Brick"])
    plain_b = _torrent("Dune by Frank Herbert", narrators=["Scott Brick"])
    plain_b.guid = "second"

    result = strict_matches(book, [full_cast, wordy, plain_a, plain_b])
    assert result == [plain_a, plain_b, wordy, full_cast]


def test_release_with_the_whole_title_beats_one_with_only_the_series() -> None:
    book = _book("Star Wars: Thrawn", ["Timothy Zahn"])
    other = _torrent("Timothy Zahn - Star Wars - Heir to the Empire")
    right = _torrent("Timothy Zahn - Star Wars - Thrawn (Unabridged)")
    assert strict_matches(book, [other, right]) == [right, other]


def test_minimum_seeders_only_applies_to_torrents() -> None:
    book = _book("Dune", ["Frank Herbert"])
    dead = _torrent("Frank Herbert - Dune", seeders=1)
    usenet = UsenetSource(
        guid="usenet",
        indexer_id=2,
        indexer="MockUsenet",
        title="Frank Herbert - Dune",
        size=100_000_000,
        publish_date=datetime(2024, 1, 1, tzinfo=UTC),
        info_url=None,
        indexer_flags=[],
        grabs=0,
    )
    assert strict_matches(book, [dead, usenet], min_seeders=2) == [usenet]


def test_no_author_means_no_strict_match() -> None:
    book = _book("Dune", [])
    assert strict_matches(book, [_torrent("Dune")]) == []


# -- fallback search -------------------------------------------------------
def _run_choose(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    book: Audiobook,
    results: dict[str, list[ProwlarrSource]],
) -> tuple[auto.AutoPick, list[str]]:
    """Runs choose_auto_download against canned Prowlarr results per query."""
    queries: list[str] = []

    async def fake_search(
        session: Session,
        client_session: ClientSession,
        book: Book,
        query: str,
        force_refresh: bool,
    ) -> list[ProwlarrSource]:
        _ = session, client_session, book, force_refresh
        queries.append(query)
        return results.get(query, [])

    monkeypatch.setattr(auto, "_search", fake_search)
    monkeypatch.setattr(auto, "FALLBACK_QUERY_DELAY_SECONDS", 0)

    async def run() -> auto.AutoPick:
        async with ClientSession() as client_session:
            return await auto.choose_auto_download(db, client_session, book, [])

    return asyncio.run(run()), queries


def test_fallback_finds_release_by_title_and_narrator(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_config.set_strict(db, True)
    book = _book("Dune: Book One", ["Frank Herbert"], ["Scott Brick"])
    release = _torrent("Frank Herbert - Dune")
    pick, queries = _run_choose(db, monkeypatch, book, {"Dune Scott Brick": [release]})

    assert pick.source == release
    assert pick.query == "Dune Scott Brick"
    assert queries == ["Dune Scott Brick"]


def test_fallback_by_author_leaves_unverified_narrator_for_review(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_config.set_strict(db, True)
    book = _book("Dune: Book One", ["Frank Herbert"], ["Scott Brick"])
    release = _torrent("Frank Herbert - Dune")
    results: dict[str, list[ProwlarrSource]] = {"Dune herbert": [release]}

    pick, queries = _run_choose(db, monkeypatch, book, results)
    assert pick.source is None
    assert pick.strict
    assert "narrator is unverified" in pick.note
    assert queries == ["Dune Scott Brick", "Dune herbert"]

    match_config.set_allow_unverified_narrator(db, True)
    pick, _ = _run_choose(db, monkeypatch, book, results)
    assert pick.source == release
    assert pick.query == "Dune herbert"


def test_fallback_can_be_turned_off(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_config.set_strict(db, True)
    match_config.set_fallback_search(db, False)
    book = _book("Dune", ["Frank Herbert"])
    pick, queries = _run_choose(db, monkeypatch, book, {})
    assert pick.source is None
    assert queries == []


def test_strict_off_keeps_the_best_ranked_source(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_config.set_strict(db, False)
    book = _book("Dune", ["Frank Herbert"])
    unrelated = _torrent("Something else entirely")

    async def run() -> auto.AutoPick:
        async with ClientSession() as client_session:
            return await auto.choose_auto_download(
                db, client_session, book, [unrelated]
            )

    pick = asyncio.run(run())
    assert pick.source == unrelated
    assert not pick.strict
    _ = monkeypatch


# -- through the API -------------------------------------------------------
def _grabs(prowlarr: MockServer) -> list[bytes]:
    return [
        r["body"]
        for r in prowlarr.requests_to("/prowlarr/api/v1/search")
        if r["method"] == "POST"
    ]


def test_auto_download_refuses_a_loose_match(
    admin_client: TestClient,
    prowlarr: MockServer,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auto, "FALLBACK_QUERY_DELAY_SECONDS", 0)
    match_config.set_strict(db, True)
    # the default mock release is "Test Book m4b": right title, no author

    response = admin_client.post(f"/api/requests/{ASIN}/auto-download")
    assert response.status_code == 404
    assert "No release matched" in response.text
    assert _grabs(prowlarr) == []

    book = db.get(Audiobook, ASIN)
    assert book is not None and not book.fulfilled

    searches = [
        r["query"]["query"]
        for r in prowlarr.requests_to("/prowlarr/api/v1/search")
        if r["method"] == "GET"
    ]
    assert searches == ["Test Book", "Test Book Mock Narrator", "Test Book author"]


def test_auto_download_grabs_a_strict_match(
    admin_client: TestClient,
    prowlarr: MockServer,
    db: Session,
) -> None:
    match_config.set_strict(db, True)
    loose = dict(prowlarr.state["search_results"][0])
    strict = dict(loose)
    strict["guid"] = "mock-guid-strict"
    strict["title"] = "Mock Author - Test Book [m4b]"
    strict["seeders"] = 5
    prowlarr.state["search_results"] = [loose, strict]

    response = admin_client.post(f"/api/requests/{ASIN}/auto-download")
    assert response.status_code == 204
    grabs = _grabs(prowlarr)
    assert len(grabs) == 1
    assert b"mock-guid-strict" in grabs[0]

    db.expire_all()
    book = db.get(Audiobook, ASIN)
    assert book is not None and book.fulfilled


def test_matching_settings_api_and_page(admin_client: TestClient) -> None:
    response = admin_client.get("/api/settings/matching")
    assert response.status_code == 200
    assert response.json() == {
        "strict": False,
        "require_narrator": False,
        "allow_unverified_narrator": False,
        "fallback_search": True,
    }

    response = admin_client.post(
        "/settings/matching/hx-form",
        data={"strict": "on", "require_narrator": "on"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'id="matching-settings"' in response.text

    assert admin_client.get("/api/settings/matching").json() == {
        "strict": True,
        "require_narrator": True,
        "allow_unverified_narrator": False,
        "fallback_search": False,
    }

    response = admin_client.get("/settings/matching/hx-form")
    assert response.status_code == 200
    assert "Strict matching" in response.text


def test_matching_settings_require_admin(trusted_client: TestClient) -> None:
    assert trusted_client.get("/api/settings/matching").status_code == 403
    response = trusted_client.patch(
        "/api/settings/matching",
        json={
            "strict": False,
            "require_narrator": False,
            "allow_unverified_narrator": False,
            "fallback_search": False,
        },
    )
    assert response.status_code == 403
