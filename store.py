"""Persistence.

``jobs.json`` holds all jobs with their status and generated email,
``profile.json`` the applicant profile the emails are written from.

Instead of a database, the app keeps everything in two JSON files. That is
enough for one user and makes the data easy to look at. Two things make it
reliable:

* A lock, so two threads (e.g. two browser requests) never read and write
  the file at the same time and overwrite each other's changes.
* Atomic writes (``_write_json``), so a crash never leaves a broken file.

Shape of ``jobs.json``::

    {
      "generated_at": "2026-11-03 15:10:00",
      "query": {...search settings...},
      "count": 83,
      "jobs": [{"refnr": "...", "title": "...", "status": "new", ...}, ...]
    }
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from emails import build_subject, clean_title
from jobboard import DEFAULT_LOCATION, DEFAULT_RADIUS, mentions_minijob

# ``Path(__file__)`` is this file; ``with_name`` swaps the file name, so the
# JSON files always sit next to the code, no matter where you start it from.
OUTPUT_FILE = Path(__file__).with_name("jobs.json")
PROFILE_FILE = Path(__file__).with_name("profile.json")
STATUSES = ("new", "applied", "hidden")

# Used until a profile is saved, and for fields missing in profile.json. Empty
# on purpose: without a name, ``minijob.require_profile`` blocks writing emails.
DEFAULT_PROFILE = {
    "name": "",
    "city": "",
    "occupation": "",
    "skills": "",
    "availability": "",
    "about": "",
}
MAX_PROFILE_FIELD_LENGTH = 1000

# RLock = re-entrant lock: the thread holding it may take it again.
# ``update()`` holds the lock and then calls ``load_state()``, which takes it
# once more. A plain Lock would wait for itself there and freeze forever.
_lock = threading.RLock()


# --- Jobs -------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Read jobs.json. Returns an empty state if the file is missing or broken."""
    data: dict[str, Any] = {}
    with _lock:
        if OUTPUT_FILE.exists():
            try:
                data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Keep a damaged file as a backup instead of overwriting it.
                try:
                    OUTPUT_FILE.replace(OUTPUT_FILE.with_suffix(".json.bak"))
                except OSError:
                    pass
    jobs = [normalize(j) for j in data.get("jobs", []) if isinstance(j, dict) and j.get("refnr")]
    return {
        "generated_at": data.get("generated_at"),
        # The last search; "wo" and "umkreis" are also the remembered choice.
        "query": {"wo": DEFAULT_LOCATION, "umkreis": DEFAULT_RADIUS, **(data.get("query") or {})},
        "count": len(jobs),
        "jobs": jobs,
    }


def save_state(state: dict[str, Any]) -> None:
    state["count"] = len(state["jobs"])
    _write_json(OUTPUT_FILE, state)


@contextmanager
def update() -> Iterator[dict[str, Any]]:
    """Load the state, let the caller modify it, then save it, all under one lock.

    Usage::

        with store.update() as state:
            state["jobs"][0]["status"] = "applied"
        # saved here, when the with-block ends

    ``@contextmanager`` turns this generator into something usable with
    ``with``: the code before ``yield`` runs on entering the block, the code
    after it on leaving. If the block raises an exception, the code after
    ``yield`` is skipped, so a failed change is never saved.
    """
    with _lock:
        state = load_state()
        yield state
        save_state(state)


def find(state: dict[str, Any], refnr: str) -> dict[str, Any]:
    """The job with this refnr. Raises KeyError, which app.py turns into a 404."""
    for job in state["jobs"]:
        if job["refnr"] == refnr:
            return job
    raise KeyError(refnr)


def set_status(refnr: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"Ungültiger Status: {status}")
    with update() as state:
        job = find(state, refnr)
        job["status"] = status
    return job


def normalize(job: dict[str, Any]) -> dict[str, Any]:
    """Fill in default fields and derive the email subject and Minijob check.

    Runs on every job that is loaded or newly fetched, so the rest of the code
    can rely on all fields being present, even in files from older versions.
    Derived fields are recomputed each time and therefore never outdated.
    """
    if job.get("status") not in STATUSES:
        job["status"] = "new"
    job["application_email"] = (job.get("application_email") or "").strip()
    if job["application_email"]:
        job.setdefault("email_source", "ollama")
    else:
        job["email_source"] = None
    # ``setdefault`` only sets the value if the key does not exist yet.
    job.setdefault("email_note", None)
    # Older files may hold addresses with a sentence-ending dot ("a@b.de.").
    job["contact_email"] = (job.get("contact_email") or "").strip().rstrip(".") or None
    job["application_subject"] = build_subject(job)
    job["display_title"] = clean_title(job.get("title"))  # short title for the list
    job["minijob_mentioned"] = mentions_minijob(job)
    job.pop("email_error", None)  # field of an old version, no longer used
    return job


# --- Profile ----------------------------------------------------------------


def load_profile() -> dict[str, str]:
    """The saved profile; fields that were never saved fall back to the defaults."""
    data: Any = {}
    with _lock:
        if PROFILE_FILE.exists():
            try:
                data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
    if not isinstance(data, dict):
        data = {}
    # Dict comprehension: builds {key: value} for every field in DEFAULT_PROFILE,
    # so unknown keys in the file are ignored and missing ones get the default.
    return {key: str(data.get(key, default)).strip() for key, default in DEFAULT_PROFILE.items()}


def save_profile(data: Any) -> dict[str, str]:
    """Validate and save the profile sent by the dashboard.

    The input comes from the browser, so it is never trusted: only known
    fields are kept, everything is converted to trimmed text with a length
    limit, and a missing name is rejected with a ValueError (HTTP 400).
    """
    if not isinstance(data, dict):
        raise ValueError("Ungültige Profildaten.")
    profile = {
        key: str(data.get(key) or "").strip()[:MAX_PROFILE_FIELD_LENGTH]
        for key in DEFAULT_PROFILE
    }
    if not profile["name"]:
        raise ValueError("Bitte gib deinen Namen an.")
    _write_json(PROFILE_FILE, profile)
    return profile


# --- Helpers ----------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    """Write atomically, so a crash never leaves a half-written file.

    The data first goes into a temporary file. ``os.replace`` then swaps it in
    with one operation of the file system: afterwards the file is either
    completely old or completely new, never half of each.
    """
    with _lock:
        tmp = path.with_suffix(".json.tmp")
        # ensure_ascii=False keeps umlauts readable instead of "ü".
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
