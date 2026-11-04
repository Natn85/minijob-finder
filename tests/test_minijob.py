"""Tests for the Minijob finder and its Flask API.

Run from the project folder:  .venv\\Scripts\\python.exe -m pytest
No network, no Ollama needed: the job board and the model are faked.
"""

import json

import pytest
import requests

import app as web
import emails
import jobboard
import minijob
import store
from conftest import raw_listing


@pytest.fixture
def client():
    """Flask's test client sends requests to the app without starting a server."""
    return web.app.test_client()


# --- text helpers -----------------------------------------------------------


# parametrize runs the same test once per tuple in the list.
@pytest.mark.parametrize("raw, clean", [
    ("Minijob Reinigungskraft 97072 Würzburg (m/w/d)", "Reinigungskraft"),
    ("Sicherheitsmitarbeiter (m/w/d) auf Minijob-Basis", "Sicherheitsmitarbeiter auf Minijob-Basis"),
    ("Aushilfe für die Warenverräumung (w/m/d) 6 Std./Wo. in 97070 Würzburg",
     "Aushilfe für die Warenverräumung 6 Std./Wo."),
    ("Hausmeister/Gärtner (w/m/d) für unsere Kitas in Würzburg-Heuchelhof",
     "Hausmeister/Gärtner für unsere Kitas in Würzburg-Heuchelhof"),
    ("Servicekraft ? (w/m/d) ? Events & Veranstaltungen in Würzburg", "Servicekraft Events & Veranstaltungen"),
])
def test_clean_title_keeps_sentences_intact(raw, clean):
    assert emails.clean_title(raw) == clean


def test_subject_does_not_repeat_minijob():
    assert emails.build_subject({"title": "Küchenhilfe (m/w/d)"}) == "Bewerbung: Küchenhilfe (Minijob)"
    assert emails.build_subject({"title": "Fahrer auf Minijob-Basis"}) == "Bewerbung: Fahrer auf Minijob-Basis"


def test_template_quotes_title_and_has_no_placeholders():
    text = emails.build_template_email({"title": "Wir suchen eine Reinigungskraft", "company": "Firma X"},
                                       store.load_profile())
    assert "„Wir suchen eine Reinigungskraft“ bei Firma X" in text
    assert text.endswith("Max Testmann") and "[" not in text


@pytest.mark.parametrize("title, excluded", [
    ("Pflegefachkraft Intensivpflege (m/w/d)", True),
    ("Student*in - Kasse & Lager", True),
    ("Studentische Hilfskraft / Aushilfe Kita", False),   # "Aushilfe" wins
    ("Mitarbeiter Rezeption, Werkstudent / Minijob", False),
    ("Reinigungskraft", False),
    ("Physiotherapeut/in auf Minijob-Basis", True),       # a qualification always wins
    ("Rezeption in Physiotherapiepraxis", False),
])
def test_exclude_filter(title, excluded):
    assert jobboard.is_excluded(title) is excluded


def test_contact_email_without_trailing_dot():
    assert jobboard._extract_email({}, "Bewerbung an info@fewue.de.") == "info@fewue.de"
    assert store.normalize({"refnr": "1", "title": "x", "contact_email": "a@b.de."})["contact_email"] == "a@b.de"


def test_clean_email_strips_model_noise():
    text = emails.clean_email("```\nBetreff: Bewerbung\n**Sehr geehrte Damen und Herren,**\n\nText.\n\nMit freundlichen Grüßen\n```",
                              "Max Testmann")
    assert "Betreff" not in text and "**" not in text and "```" not in text
    assert text.endswith("Mit freundlichen Grüßen\nMax Testmann")


@pytest.mark.parametrize("title, description, mentioned", [
    ("Reinigungskraft auf 633€-Basis", "", True),
    ("Verkäufer/in (m/w/d)", "Wir bieten eine Anstellung als geringfügig Beschäftigte/r.", True),
    ("Verkäufer/in (m/w/d)", "Vollzeit oder Teilzeit.", False),
])
def test_minijob_mentioned_flag(title, description, mentioned):
    assert store.normalize({"refnr": "1", "title": title, "description": description})["minijob_mentioned"] is mentioned


# --- refresh / state --------------------------------------------------------


def test_refresh_merges_and_keeps_status(board):
    board.listings = [raw_listing("A", "Küchenhilfe (m/w/d)"), raw_listing("B", "Reinigungskraft")]
    board.details = {"A": {"stellenangebotsBeschreibung": "Bewerbung an jobs@kueche.de."}}
    minijob.refresh_jobs(verbose=False)
    assert board.detail_calls == ["A", "B"]
    assert minijob.get_job("A")["contact_email"] == "jobs@kueche.de"

    store.set_status("A", "applied")
    store.set_status("B", "hidden")
    board.detail_calls.clear()
    board.listings = [raw_listing("B", "Reinigungskraft"), raw_listing("C", "Lagerhelfer")]
    state = minijob.refresh_jobs(verbose=False)

    ids = [j["refnr"] for j in state["jobs"]]
    assert board.detail_calls == ["C"]                # only new listings are fetched
    assert ids[0] == "C"                              # new first
    assert "A" in ids                                 # applied survives even though gone
    assert minijob.get_job("B")["status"] == "hidden" # status kept


def test_refresh_parses_v6_fields(board):
    board.listings = [raw_listing("A", "Küchenhilfe (m/w/d)", "Hotel Test", plz="97072")]
    job = minijob.refresh_jobs(verbose=False)["jobs"][0]
    assert (job["title"], job["company"], job["location"]) == ("Küchenhilfe (m/w/d)", "Hotel Test", "97072 Würzburg (Bayern)")
    assert job["application_url"].endswith("/jobdetail/A")


def test_refresh_drops_excluded_and_expired(board):
    board.listings = [raw_listing("A", "Lehrkraft Mathe"), raw_listing("B", "Aushilfe Kasse")]
    minijob.refresh_jobs(verbose=False)
    board.listings = []
    state = minijob.refresh_jobs(verbose=False)
    assert state["jobs"] == []


def test_corrupt_file_is_backed_up_not_overwritten(isolated_state):
    isolated_state.write_text("{kaputt", encoding="utf-8")
    assert store.load_state()["jobs"] == []
    assert isolated_state.with_suffix(".json.bak").exists()


def test_legacy_file_loads(isolated_state):
    isolated_state.write_text(json.dumps({"jobs": [{"refnr": "X", "title": "Minijob Koch (m/w/d)",
                                                    "application_email": "Alt", "email_error": "x"}]}),
                              encoding="utf-8")
    job = store.load_state()["jobs"][0]
    assert job["status"] == "new" and job["email_source"] == "ollama"
    assert "email_error" not in job and job["application_subject"] == "Bewerbung: Koch (Minijob)"


# --- email generation -------------------------------------------------------


@pytest.fixture
def one_job(board):
    board.listings = [raw_listing("A", "Rezeption (m/w/d)", "Hotel Test")]
    minijob.refresh_jobs(verbose=False)
    return "A"


GOOD = ("Sehr geehrte Damen und Herren,\n\nhiermit bewerbe ich mich auf Ihre Stelle an der Rezeption. "
        "Ich suche einen zuverlässigen Minijob neben meiner Ausbildung.\n\nMit freundlichen Grüßen")


@pytest.mark.parametrize("exc, note_part", [
    (requests.ConnectionError("x"), "läuft nicht"),
    (requests.Timeout("x"), "zu lange"),
])
def test_template_fallback_with_reason(one_job, monkeypatch, exc, note_part):
    def boom(job, profile):
        raise exc
    monkeypatch.setattr(emails, "_ollama_email", boom)
    job = minijob.ensure_email(one_job)
    assert job["email_source"] == "template" and note_part in job["email_note"]


def test_model_email_is_cached(one_job, monkeypatch):
    monkeypatch.setattr(emails, "_ollama_email", lambda job, profile: GOOD)
    first = minijob.ensure_email(one_job)
    assert first["email_source"] == "ollama" and first["application_email"].endswith("Max Testmann")
    monkeypatch.setattr(emails, "_ollama_email", lambda job, profile: pytest.fail("regenerated"))
    assert minijob.ensure_email(one_job)["application_email"] == first["application_email"]


def test_placeholders_are_rejected(one_job, monkeypatch):
    monkeypatch.setattr(emails, "_ollama_email", lambda job, profile: "Hallo [Firma], " + GOOD)
    assert minijob.ensure_email(one_job)["email_source"] == "template"


def test_email_uses_saved_profile(one_job, monkeypatch):
    store.save_profile({"name": "Lena Schmidt", "city": "Würzburg"})
    seen = {}

    def fake(job, profile):
        seen["prompt"] = emails.build_system_prompt(profile)
        return GOOD
    monkeypatch.setattr(emails, "_ollama_email", fake)
    job = minijob.ensure_email(one_job)
    assert "- Name: Lena Schmidt" in seen["prompt"] and "- Kenntnisse:" not in seen["prompt"]  # empty field left out
    assert job["application_email"].endswith("Lena Schmidt")


def test_stream_email_yields_tokens_then_stored_job(one_job, monkeypatch):
    monkeypatch.setattr(emails, "ollama_stream", lambda job, profile: iter(["Betreff: x\n", GOOD[:40], GOOD[40:]]))
    events = list(minijob.stream_email(one_job))
    assert [e["type"] for e in events] == ["start", "token", "token", "token", "done"]
    final = events[-1]["job"]
    assert "Betreff" not in final["application_email"]     # cleaned version is final
    assert minijob.get_job(one_job)["application_email"] == final["application_email"]


def test_stream_email_falls_back_mid_stream(one_job, monkeypatch):
    def broken(job, profile):
        yield "Sehr geehrte"
        raise requests.Timeout("x")
    monkeypatch.setattr(emails, "ollama_stream", broken)
    final = list(minijob.stream_email(one_job))[-1]["job"]
    assert final["email_source"] == "template" and "zu lange" in final["email_note"]


# --- HTTP API ---------------------------------------------------------------


def test_api_status_validation(client, one_job):
    assert client.post(f"/api/jobs/{one_job}/status", json={"status": "applied"}).get_json()["status"] == "applied"
    assert client.post(f"/api/jobs/{one_job}/status", json={"status": "egal"}).status_code == 400
    assert client.post("/api/jobs/gibtsnicht/status", json={"status": "new"}).status_code == 404


def test_api_stream_is_ndjson(client, one_job, monkeypatch):
    monkeypatch.setattr(emails, "ollama_stream", lambda job, profile: iter([GOOD]))
    resp = client.post(f"/api/jobs/{one_job}/email/stream", json={})
    assert resp.mimetype == "application/x-ndjson"
    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    assert events[0]["type"] == "start" and events[-1]["type"] == "done"
    assert client.post("/api/jobs/gibtsnicht/email/stream", json={}).status_code == 404


def test_api_stream_needs_a_name(client, one_job, isolated_state):
    store.PROFILE_FILE.write_text('{"name": ""}', encoding="utf-8")
    resp = client.post(f"/api/jobs/{one_job}/email/stream", json={})
    assert resp.status_code == 400 and "Namen" in resp.get_json()["error"]


def test_new_install_has_empty_profile_and_needs_a_name(client, one_job, isolated_state):
    store.PROFILE_FILE.unlink()
    assert not any(client.get("/api/profile").get_json().values())
    resp = client.post(f"/api/jobs/{one_job}/email/stream", json={})
    assert resp.status_code == 400 and "Namen" in resp.get_json()["error"]


def test_api_profile_roundtrip_and_validation(client):
    assert client.get("/api/profile").get_json()["name"] == "Max Testmann"
    saved = client.put("/api/profile", json={"name": " Lena ", "skills": "Kasse", "extra": "ignored"}).get_json()
    assert saved["name"] == "Lena" and saved["skills"] == "Kasse" and "extra" not in saved
    assert client.get("/api/profile").get_json() == saved
    assert client.put("/api/profile", json={"name": "  "}).status_code == 400
    assert client.put("/api/profile", data="x", content_type="text/plain").status_code == 400


def test_api_refresh_reports_board_outage(client, monkeypatch):
    def down(location, radius):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(jobboard, "search", down)
    resp = client.post("/api/refresh")
    assert resp.status_code == 502 and "Jobbörse" in resp.get_json()["error"]


def test_api_refresh_sends_chosen_location_and_radius(client, board):
    board.listings = [raw_listing("A", "Küchenhilfe")]
    state = client.post("/api/refresh", json={"location": " 10115 ", "radius": "50"}).get_json()
    assert (board.search_params[0]["wo"], board.search_params[0]["umkreis"]) == ("10115", "50")
    assert (state["query"]["wo"], state["query"]["umkreis"]) == ("10115", "50")


def test_search_choice_is_remembered_and_defaults_to_wuerzburg(client, board):
    query = client.get("/api/jobs").get_json()["query"]
    assert (query["wo"], query["umkreis"]) == ("Würzburg", "25")

    client.post("/api/refresh", json={"location": "Berlin", "radius": "100"})
    query = client.get("/api/jobs").get_json()["query"]  # read back from jobs.json
    assert (query["wo"], query["umkreis"]) == ("Berlin", "100")

    board.search_params.clear()
    client.post("/api/refresh")                          # no choice sent: keeps the last one
    assert (board.search_params[0]["wo"], board.search_params[0]["umkreis"]) == ("Berlin", "100")


@pytest.mark.parametrize("body", [{"location": "   ", "radius": "25"}, {"location": "Berlin", "radius": "7"}])
def test_api_refresh_rejects_invalid_choice_without_searching(client, board, body):
    assert client.post("/api/refresh", json=body).status_code == 400
    assert board.search_params == []


def test_display_title_is_clean(isolated_state):
    isolated_state.write_text(json.dumps({"jobs": [{"refnr": "X", "title": "Minijob Koch 97070 Würzburg (m/w/d)"}]}),
                              encoding="utf-8")
    assert store.load_state()["jobs"][0]["display_title"] == "Koch"
