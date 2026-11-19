"""Search filters, the series view and the series watch list."""

from __future__ import annotations

import asyncio
import base64
import copy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Self, cast

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

import app.main
from app.internal.audible import search as audible_search
from app.internal.audible.types import AudibleProduct
from app.internal.library.index import library_index
from app.internal.models import Audiobook, AudiobookRequest, GroupEnum, User
from app.internal.ranking.quality import quality_config
from app.internal.retry.pool import get_rows
from app.internal.series import audible as series_audible
from app.internal.series import job
from app.internal.series.audible import SeriesBook, get_series
from app.internal.series.config import SeriesMode, series_config
from app.internal.series.memo import series_of
from app.internal.series.plan import build_plan
from tests.http import as_dict, as_list, json_object
from tests.mock_services import MockServer

SERIES = "B0SERIES01"
TITLE = "The Long Road"
PAST = "2020-01-01"
FUTURE = (datetime.now(UTC) + timedelta(days=90)).strftime("%Y-%m-%d")


def _product(
    asin: str,
    sequence: str | None,
    title: str | None = None,
    release: str = PAST,
    narrator: str = "Usual Reader",
    language: str = "english",
    runtime: int = 600,
    format_type: str = "unabridged",
    series: str = SERIES,
) -> dict[str, object]:
    return {
        "asin": asin,
        "title": title or f"Road Book {sequence}",
        "authors": [{"name": "Ann Author"}],
        "narrators": [{"name": narrator}],
        "product_images": {"500": f"https://img.example/{asin}.jpg"},
        "runtime_length_min": runtime,
        "release_date": release,
        "language": language,
        "format_type": format_type,
        "series": [{"asin": series, "title": TITLE, "sequence": sequence or ""}],
    }


PRODUCTS: list[dict[str, object]] = [
    _product("B0ROAD0001", "1"),
    _product("B0ROAD0002", "2"),
    _product("B0ROAD0003", "3"),
    _product("B0ROAD0004", "4"),
    _product("B0ROAD0005", "5", release=FUTURE),
    _product("B0ROADBOX1", "1-3", title="The Long Road: Books 1-3"),
    _product("B0ROAD002D", "2", title="Road Book 2 (German)", language="german"),
    _product("B0ROAD004A", "4", narrator="Someone Else", runtime=300),
    _product("B0ROADXTRA", None, title="Road Stories"),
    # a book of another series that a keyword search also turns up
    _product("B0OTHER001", "1", title="Other Road", series="B0SERIES02"),
]


def _book(product: dict[str, object]) -> SeriesBook:
    parsed = AudibleProduct.model_validate(product)
    ref = parsed.series_ref()
    return series_audible._to_book(parsed, ref.sequence if ref else None)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def audible(
    mock_server: MockServer, monkeypatch: pytest.MonkeyPatch, db: Session
) -> Iterator[MockServer]:
    def mock_base(region: str) -> str:
        _ = region
        return f"{mock_server.base_url}/audible"

    monkeypatch.setattr(series_audible, "api_base", mock_base)

    async def no_search(asin: str, auto_download: bool) -> None:
        _ = auto_download
        started.append(asin)

    monkeypatch.setattr(job, "background_start_query", no_search)
    started.clear()
    mock_server.state["audible_products"] = copy.deepcopy(PRODUCTS)
    mock_server.state["audible_series"] = {
        SERIES: {
            "asin": SERIES,
            "title": TITLE,
            "relationships": [
                {
                    "asin": p["asin"],
                    "relationship_to_product": "child",
                    "relationship_type": "series",
                    "sequence": cast(list[dict[str, str]], p["series"])[0]["sequence"],
                }
                for p in PRODUCTS
                if p["asin"] != "B0OTHER001"
            ]
            + [{"asin": "B0PARENT", "relationship_to_product": "parent"}],
        }
    }
    library_index.clear()
    _ = db
    yield mock_server
    library_index.clear()


started: list[str] = []


@pytest.fixture
def untrusted_client() -> Iterator[TestClient]:
    token = base64.b64encode(b"untrusted:untrustedpass").decode()
    with TestClient(
        app.main.app,
        headers={"Authorization": f"Basic {token}"},
        raise_server_exceptions=False,
    ) as client:
        yield client


def _own(db: Session, *asins: str) -> None:
    """Books that are already fulfilled count as in the library."""
    for asin in asins:
        product = next(p for p in PRODUCTS if p["asin"] == asin)
        book = _book(product).to_audiobook()
        book.fulfilled = True
        db.add(book)
    db.commit()


def _requested(db: Session, username: str | None = None) -> set[str]:
    query = select(AudiobookRequest.asin)
    if username:
        query = query.where(AudiobookRequest.user_username == username)
    return {a for a in db.exec(query).all() if a.startswith("B0ROAD")}


# -- the plan --------------------------------------------------------------
def _plan(mode: str, owned: set[str], asked: set[str] | None = None):
    return build_plan(
        [_book(p) for p in PRODUCTS if p["asin"] != "B0OTHER001"],
        cast(SeriesMode, mode),
        lambda b: b.asin in owned,
        lambda b: b.asin in (asked or set()),
        watch_since=datetime.now(UTC),
        language_region="us",
    )


def test_fill_in_asks_for_every_missing_book() -> None:
    plan = _plan("fill", {"B0ROAD0002"})
    assert [b.asin for b in plan.request] == ["B0ROAD0001", "B0ROAD0003", "B0ROAD0004"]
    assert [b.asin for b in plan.upcoming] == ["B0ROAD0005"]
    reasons = {i.book.asin: i.reason for i in plan.skipped}
    assert reasons["B0ROADBOX1"] == "omnibus or box set"
    assert reasons["B0ROADXTRA"] == "not numbered"
    assert reasons["B0ROAD002D"] == "other language"
    # the edition read by the usual narrator wins over the other one
    assert reasons["B0ROAD004A"] == "another edition of book 4"
    assert plan.upcoming_text is not None and plan.upcoming_text.startswith("Book 5")


def test_new_only_starts_after_the_newest_owned_book() -> None:
    plan = _plan("new", {"B0ROAD0001", "B0ROAD0003"})
    assert [b.asin for b in plan.request] == ["B0ROAD0004"]
    reasons = {i.book.asin: i.reason for i in plan.skipped}
    assert reasons["B0ROAD0002"] == "before your newest book (New only)"


def test_new_only_with_nothing_owned_waits_for_new_releases() -> None:
    plan = _plan("new", set())
    assert plan.request == []
    assert [b.asin for b in plan.upcoming] == ["B0ROAD0005"]


def test_other_edition_of_an_owned_book_is_left_alone() -> None:
    plan = _plan("fill", {"B0ROAD004A"}, asked={"B0ROAD0001"})
    assert [b.asin for b in plan.request] == ["B0ROAD0002", "B0ROAD0003"]
    reasons = {i.book.asin: i.reason for i in plan.skipped}
    assert reasons["B0ROAD0004"] == "another edition of book 4"
    assert {i.book.asin: i.reason for i in plan.have} == {
        "B0ROAD0001": "requested",
        "B0ROAD004A": "in library",
    }


def test_omnibus_detection() -> None:
    def book(title: str, sequence: str | None) -> SeriesBook:
        return _book(_product("B0X", sequence, title=title))

    assert book("Saga: Books 1-3", "1-3").is_omnibus
    assert book("Saga Box Set", "1").is_omnibus
    assert book("The Saga Omnibus", None).is_omnibus
    assert book("Saga, Volumes 1 & 2", "1").is_omnibus
    assert not book("Book 12", "12").is_omnibus
    assert not book("A Novella", "2.5").is_omnibus
    assert book("A Novella", "2.5").number_text == "2.5"


# -- listing a series ------------------------------------------------------
def _get(force: bool = True):
    async def run():
        async with ClientSession() as client_session:
            return await get_series(client_session, SERIES, "us", TITLE, force=force)

    return asyncio.run(run())


def test_series_is_listed_from_its_relationships(audible: MockServer) -> None:
    info = _get()
    assert info is not None
    assert info.title == TITLE
    assert info.source == "relationships"
    assert [b.asin for b in info.books][:3] == [
        "B0ROAD0001",
        "B0ROAD0002",
        "B0ROAD002D",
    ]
    assert info.books[-2].asin == "B0ROADBOX1"
    assert "B0OTHER001" not in {b.asin for b in info.books}
    assert info.books[-1].asin == "B0ROADXTRA"
    # a second look within the hour does not ask Audible again
    audible.requests.clear()
    assert _get(force=False) is not None
    assert audible.requests == []


def test_series_falls_back_to_a_search(audible: MockServer) -> None:
    audible.state["audible_relationships_status"] = 500
    info = _get()
    assert info is not None
    assert info.source == "search"
    assert "B0OTHER001" not in {b.asin for b in info.books}
    assert len(info.books) == len(PRODUCTS) - 1

    audible.state["audible_products"] = []
    assert _get() is None


# -- watching --------------------------------------------------------------
def test_watch_previews_then_confirm_requests(
    db: Session, trusted_client: TestClient
) -> None:
    _own(db, "B0ROAD0002")
    response = trusted_client.post(f"/api/series/{SERIES}/watch", json={"mode": "fill"})
    assert response.status_code == 200
    view = json_object(response)
    plan = as_dict(view["plan"])
    assert [as_dict(b)["asin"] for b in as_list(plan["request"])] == [
        "B0ROAD0001",
        "B0ROAD0003",
        "B0ROAD0004",
    ]
    assert as_dict(view["watched"])["confirmed"] is False
    # only a preview so far
    assert _requested(db) == set()

    response = trusted_client.post(f"/api/series/{SERIES}/confirm")
    assert response.status_code == 200
    db.expire_all()
    assert _requested(db, "series-watch") == {
        "B0ROAD0001",
        "B0ROAD0003",
        "B0ROAD0004",
    }
    user = db.get(User, "series-watch")
    assert user is not None and user.group == GroupEnum.trusted
    # trusted, so the requests are searched without an approval
    stages = {r.result.book.asin: r.stage for r in get_rows(db)}
    assert stages["B0ROAD0003"] == "searching"

    watched = series_config.get_series(db, SERIES)
    assert watched is not None and watched.confirmed
    assert watched.added_by == "trusted"
    assert len(watched.requested) == 3
    assert watched.upcoming is not None and watched.upcoming.startswith("Book 5")


def test_confirm_starts_the_searches_when_auto_download_is_on(
    db: Session, trusted_client: TestClient
) -> None:
    quality_config.set_auto_download(db, True)
    _own(db, "B0ROAD0001", "B0ROAD0002", "B0ROAD0003")
    assert (
        trusted_client.post(
            f"/api/series/{SERIES}/watch", json={"mode": "new"}
        ).status_code
        == 200
    )
    assert trusted_client.post(f"/api/series/{SERIES}/confirm").status_code == 200
    assert started == ["B0ROAD0004"]


def test_daily_check_requests_a_book_once_it_is_out(
    db: Session, trusted_client: TestClient, audible: MockServer
) -> None:
    _own(db, "B0ROAD0001", "B0ROAD0002", "B0ROAD0003", "B0ROAD0004")
    _ = trusted_client.post(f"/api/series/{SERIES}/watch", json={"mode": "new"})

    # not confirmed: the check leaves it alone
    asyncio.run(job.run_series_check())
    assert _requested(db) == set()

    _ = trusted_client.post(f"/api/series/{SERIES}/confirm")
    asyncio.run(job.run_series_check())
    db.expire_all()
    assert _requested(db) == set()
    watched = series_config.get_series(db, SERIES)
    assert watched is not None and watched.last_result == "nothing new"

    # book 5 comes out
    for product in audible.state["audible_products"]:
        if product["asin"] == "B0ROAD0005":
            product["release_date"] = PAST
    asyncio.run(job.run_series_check())
    db.expire_all()
    series_config.flush()
    assert _requested(db, "series-watch") == {"B0ROAD0005"}
    watched = series_config.get_series(db, SERIES)
    assert watched is not None and watched.last_result == "requested 1 book"
    assert watched.upcoming is None

    # and is not asked for twice
    asyncio.run(job.run_series_check())
    db.expire_all()
    assert (
        len(
            db.exec(
                select(AudiobookRequest).where(
                    col(AudiobookRequest.asin) == "B0ROAD0005"
                )
            ).all()
        )
        == 1
    )


def test_daily_check_is_capped_per_series(
    db: Session, trusted_client: TestClient
) -> None:
    series_config.set_max_per_run(db, 1)
    _own(db, "B0ROAD0002")
    _ = trusted_client.post(f"/api/series/{SERIES}/watch", json={"mode": "new"})
    _ = trusted_client.post(f"/api/series/{SERIES}/confirm")
    db.expire_all()
    assert _requested(db) == {"B0ROAD0003", "B0ROAD0004"}

    # switching to Fill in needs a new confirmation before it does anything
    response = trusted_client.patch(
        f"/api/series/{SERIES}/watch", json={"mode": "fill"}
    )
    assert response.status_code == 200
    assert as_dict(json_object(response)["watched"])["confirmed"] is False
    asyncio.run(job.run_series_check())
    db.expire_all()
    assert "B0ROAD0001" not in _requested(db)


def test_daily_check_runs_once_a_day(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[int] = []

    async def fake_run(only: str | None = None) -> None:
        _ = only
        runs.append(1)
        series_config.set_last_run(db, datetime.now(UTC))

    monkeypatch.setattr(job, "run_series_check", fake_run)
    series_config.set_run_hour(db, 0)
    asyncio.run(job.scheduled_series_check())
    asyncio.run(job.scheduled_series_check())
    assert len(runs) == 1

    series_config.set_last_run(db, datetime.now(UTC) - timedelta(days=1))
    series_config.set_enabled(db, False)
    asyncio.run(job.scheduled_series_check())
    assert len(runs) == 1
    series_config.set_enabled(db, True)
    asyncio.run(job.scheduled_series_check())
    assert len(runs) == 2


def test_check_is_skipped_when_the_library_cannot_be_listed(
    db: Session, trusted_client: TestClient, abs_service: MockServer
) -> None:
    _ = trusted_client.post(f"/api/series/{SERIES}/watch", json={"mode": "fill"})
    _ = trusted_client.post(f"/api/series/{SERIES}/confirm")
    db.expire_all()
    before = _requested(db)
    assert before

    # Audiobookshelf is configured but does not answer: ask for nothing
    from app.internal.audiobookshelf.config import abs_config

    abs_config.set_base_url(db, "http://127.0.0.1:9/abs")
    library_index.clear()
    for request in db.exec(select(AudiobookRequest)).all():
        db.delete(request)
    db.commit()
    asyncio.run(job.run_series_check())
    db.expire_all()
    assert _requested(db) == set()
    _ = abs_service


def test_watching_needs_a_trusted_user(
    untrusted_client: TestClient, trusted_client: TestClient, admin_client: TestClient
) -> None:
    assert (
        untrusted_client.post(f"/api/series/{SERIES}/watch", json={}).status_code == 403
    )
    assert untrusted_client.get(f"/api/series/{SERIES}").status_code == 200
    assert trusted_client.post("/api/series/run").status_code == 403
    assert trusted_client.delete(f"/api/series/{SERIES}/watch").status_code == 404
    assert (
        trusted_client.post(f"/api/series/{SERIES}/watch", json={}).status_code == 200
    )
    assert admin_client.post("/api/series/run").status_code == 202
    listing = json_object(trusted_client.get("/api/series/watch"))
    assert len(as_list(listing["series"])) == 1
    assert trusted_client.delete(f"/api/series/{SERIES}/watch").status_code == 204
    assert trusted_client.get("/api/series/B0NOSUCH").status_code == 404


# -- pages -----------------------------------------------------------------
def test_series_pages(
    db: Session, trusted_client: TestClient, admin_client: TestClient
) -> None:
    _own(db, "B0ROAD0002")
    page = trusted_client.get(f"/series/{SERIES}?region=us&title=The+Long+Road")
    assert page.status_code == 200
    assert TITLE in page.text
    assert "Not watched" in page.text
    assert "omnibus or box set" in page.text
    assert "in library" in page.text

    body = trusted_client.post(
        f"/series/{SERIES}/hx-watch",
        data={"mode": "fill", "region": "us", "title": TITLE},
    )
    assert body.status_code == 200
    assert "Preview" in body.text
    assert "Confirm and request" in body.text
    assert _requested(db) == set()

    body = trusted_client.post(f"/series/{SERIES}/hx-confirm")
    assert body.status_code == 200
    assert "Watching" in body.text
    db.expire_all()
    assert len(_requested(db)) == 3

    listing = trusted_client.get("/series")
    assert listing.status_code == 200
    assert TITLE in listing.text
    assert "Check now" not in listing.text
    assert "Check now" in admin_client.get("/series").text
    assert admin_client.post("/series/hx-run").status_code == 200

    body = trusted_client.delete(f"/series/{SERIES}/hx-watch")
    assert body.status_code == 200
    assert "Not watched" in body.text
    assert series_config.get_series(db, SERIES) is None

    missing = trusted_client.get("/series/B0NOSUCH")
    assert missing.status_code == 200
    assert "could not be found" in missing.text


def test_series_settings(
    admin_client: TestClient, trusted_client: TestClient, db: Session
) -> None:
    assert trusted_client.get("/api/settings/series").status_code == 403
    settings = json_object(admin_client.get("/api/settings/series"))
    assert settings == {
        "enabled": True,
        "default_mode": "new",
        "run_hour": 4,
        "max_per_run": 5,
        "user": "series-watch",
        "language_filter": True,
    }
    form = admin_client.get("/settings/series/hx-form")
    assert form.status_code == 200 and "Search and series" in form.text
    response = admin_client.post(
        "/settings/series/hx-form",
        data={
            "default_mode": "fill",
            "run_hour": "2",
            "max_per_run": "0",
            "user": "robot",
            "enabled": "on",
        },
    )
    assert response.status_code == 200
    series_config.flush()
    assert series_config.get_default_mode(db) == "fill"
    assert series_config.get_run_hour(db) == 2
    assert series_config.get_user(db) == "robot"
    assert series_config.get_language_filter(db) is False
    assert admin_client.get("/settings/download").status_code == 200


# -- search filters --------------------------------------------------------
class _FakeResponse:
    status: int = 200

    def __init__(self, payload: object) -> None:
        self.payload: object = payload

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> object:
        return self.payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    def __init__(self, payload: object) -> None:
        self.payload: object = payload
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, params: dict[str, object]) -> _FakeResponse:
        _ = url
        self.calls.append(dict(params))
        return _FakeResponse(self.payload)


def _search(fake: _FakeSession, **kwargs: object) -> list[Audiobook]:
    return asyncio.run(
        audible_search.search_audible_books(
            cast(ClientSession, cast(object, fake)),
            **kwargs,  # pyright: ignore[reportArgumentType]
        )
    )


def test_search_filters_reach_audible() -> None:
    fake = _FakeSession({"products": PRODUCTS[:2] + [PRODUCTS[6]]})
    books = _search(
        fake, query="", author="Ann Author", narrator="Usual Reader", sort="newest"
    )
    assert fake.calls[0]["author"] == "Ann Author"
    assert fake.calls[0]["narrator"] == "Usual Reader"
    assert fake.calls[0]["products_sort_by"] == "-ReleaseDate"
    assert "keywords" not in fake.calls[0] and "title" not in fake.calls[0]
    # the German edition is hidden in the US store
    assert [b.asin for b in books] == ["B0ROAD0001", "B0ROAD0002"]
    # and the cards know their series
    ref = series_of("B0ROAD0002")
    assert ref is not None and ref.asin == SERIES and ref.label == f"{TITLE} #2"
    assert books[0].series is not None

    # with the language setting off the other edition is shown
    books = _search(fake, query="road", language_filter=False)
    assert fake.calls[1]["keywords"] == "road"
    assert len(books) == 3

    # nothing to search for: Audible is not asked
    assert _search(fake, query="  ") == []
    assert len(fake.calls) == 2


def test_search_page_shows_filters_and_series_link(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_search(**kwargs: object) -> list[Audiobook]:
        seen.update(kwargs)
        parsed = AudibleProduct.model_validate(PRODUCTS[0])
        return [parsed.to_audiobook()]

    import app.routers.api.search as api_search

    monkeypatch.setattr(api_search, "search_audible_books", fake_search)
    page = admin_client.get("/search?author=Ann+Author&sort=newest")
    assert page.status_code == 200
    assert seen["author"] == "Ann Author" and seen["sort"] == "newest"
    assert seen["language_filter"] is True
    assert 'value="Ann Author"' in page.text
    assert "Filters and sorting" in page.text
    assert f"/series/{SERIES}?region=us" in page.text
    assert "Road Book 1" in page.text

    api = admin_client.get("/api/search?narrator=Usual+Reader&sort=bogus")
    assert api.status_code == 422
    assert admin_client.get("/search").status_code == 200
