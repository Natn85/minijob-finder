"""Shared fixtures: every test gets its own empty jobs.json and profile, and no network.

pytest loads this file automatically. A fixture is a function whose return
value pytest passes to every test that names it as a parameter;
``autouse=True`` applies it to every test without naming it.
"""

import sys
from pathlib import Path

import pytest
import requests

# Make the project folder importable, so ``import store`` etc. work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobboard  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point jobs.json and profile.json at temp files and make any real HTTP call fail loudly.

    ``monkeypatch`` replaces an attribute for the duration of one test and
    restores it afterwards, so the real files are never touched.
    """
    monkeypatch.setattr(store, "OUTPUT_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(store, "PROFILE_FILE", tmp_path / "profile.json")

    def no_network(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    return tmp_path / "jobs.json"


def raw_listing(refnr, title, company="Test GmbH", plz="97070"):
    """A listing shaped like the Arbeitsagentur v6 search response."""
    return {"referenznummer": refnr, "stellenangebotsTitel": title, "firma": company,
            "stellenlokationen": [{"adresse": {"plz": plz, "ort": "Würzburg", "region": "BAYERN"}}]}


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def board(monkeypatch):
    """Fake job board: set ``board.listings`` / ``board.details``; records detail calls.

    Only the HTTP answer of the search is faked: the listings still go through
    the real ``jobboard.search()`` and its v6 parsing.
    """

    class Board:
        listings = []
        details = {}
        detail_calls = []
        search_params = []

    b = Board()

    def fake_get(url, **kwargs):
        if url == jobboard.SEARCH_URL:
            b.search_params.append(kwargs["params"])
            return _FakeResponse({"ergebnisliste": list(b.listings)})
        raise requests.ConnectionError(f"unexpected request in test: {url}")

    monkeypatch.setattr(requests, "get", fake_get)

    def fake_detail(refnr):
        b.detail_calls.append(refnr)
        return b.details.get(refnr, {})

    monkeypatch.setattr(jobboard, "_fetch_detail", fake_detail)
    return b
