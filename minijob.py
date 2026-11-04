"""Minijob finder: refresh listings and write application emails.

    python minijob.py            refresh jobs.json
    python minijob.py --emails   refresh and write all missing emails

The dashboard (``app.py``) uses the same functions.

This module connects the other three:

    jobboard.py  -> fetches listings from the Arbeitsagentur
    emails.py    -> writes the application emails
    store.py     -> keeps everything in jobs.json / profile.json

``store.py`` borrows two small helpers from the others (the email subject and
the Minijob check), but the workflow itself, search, load details, save and
write emails, is put together only here. That keeps each module small and
testable on its own.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

import emails
import jobboard
import store

# Ollama handles one generation at a time. If two emails were requested at
# once, the second waits here instead of slowing down both.
_generation_lock = threading.Lock()


def _search_choice(location: Any, radius: Any) -> tuple[str, str]:
    """Validated location and radius; ``None`` keeps the last choice.

    Raises ``ValueError`` (HTTP 400) for an empty location or unknown radius.
    """
    last = store.load_state()["query"]
    location = str(last["wo"] if location is None else location).strip()
    radius = str(last["umkreis"] if radius is None else radius)
    if not location:
        raise ValueError("Bitte gib einen Ort oder eine PLZ ein.")
    if radius not in jobboard.RADIUS_CHOICES:
        raise ValueError(f"Ungültiger Umkreis: {radius}")
    return location, radius


def refresh_jobs(location: Any = None, radius: Any = None, verbose: bool = True) -> dict[str, Any]:
    """Fetch the current listings and merge them into jobs.json.

    Known jobs keep their status and email, and only new listings trigger a
    detail request. Jobs marked as applied stay even after the listing is gone.

    Steps:
    1. Search the job board and drop unsuitable titles.
    2. Load details only for jobs not yet in jobs.json (saves many requests).
    3. Merge: new jobs first, then known jobs that are still online or applied.
    """

    def log(msg: str) -> None:
        if verbose:
            # flush=True prints immediately instead of when the buffer is full.
            print(msg, flush=True)

    location, radius = _search_choice(location, radius)
    log(f"Suche Minijobs in {location} ({radius} km) …")
    found = jobboard.search(location, radius)
    listings = [job for job in found if not jobboard.is_excluded(job["title"])]
    log(f"{len(found)} Stellenangebote gefunden, {len(found) - len(listings)} aussortiert.")

    # A set makes "is this refnr known?" a fast lookup instead of a list scan.
    known = {job["refnr"] for job in store.load_state()["jobs"]}
    fresh = [job for job in listings if job["refnr"] not in known]

    # Every detail request mostly waits for the network. Running 8 at the same
    # time in threads makes the refresh up to 8 times faster. ``pool.map``
    # works like the built-in ``map``, but in parallel, and returns the results
    # in the same order as the input.
    with ThreadPoolExecutor(max_workers=8) as pool:
        new_jobs = [store.normalize(job) for job in pool.map(jobboard.with_details, fresh)]
    log(f"{len(new_jobs)} neue Stellen.")

    on_board = {job["refnr"] for job in listings}
    # Load again inside update(): while we were fetching, the user may have
    # changed a status in the dashboard, and that change must not be lost.
    with store.update() as state:
        existing = {job["refnr"] for job in state["jobs"]}
        kept = [job for job in state["jobs"] if job["refnr"] in on_board or job["status"] == "applied"]
        state["jobs"] = [job for job in new_jobs if job["refnr"] not in existing] + kept
        state["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state["query"] = {**jobboard.SEARCH_PARAMS, "wo": location, "umkreis": radius, "max_pages": jobboard.MAX_PAGES}
    log(f"Gespeichert in {store.OUTPUT_FILE.name} ({len(state['jobs'])} Jobs).")
    return state


def get_job(refnr: str) -> dict[str, Any]:
    """The stored job, or ``KeyError`` (which app.py turns into a 404)."""
    return store.find(store.load_state(), refnr)


def require_profile() -> dict[str, str]:
    """The profile, or ``ValueError`` (HTTP 400) if it has no name yet."""
    profile = store.load_profile()
    if not profile["name"]:
        raise ValueError("Bitte trage zuerst im Profil deinen Namen ein.")
    return profile


def ensure_email(refnr: str, force: bool = False) -> dict[str, Any]:
    """Generate and store the email for one job, unless it already has one.

    ``force=True`` writes a new email even if one exists ("Neu schreiben").
    """
    with _generation_lock:
        job = get_job(refnr)
        if job["application_email"] and not force:
            return job
        profile = require_profile()
        # This can take a minute. The store lock is not held meanwhile, so the
        # dashboard can still load jobs and change statuses during generation.
        text, source, note = emails.generate_email(job, profile)
        return _store_email(refnr, text, source, note)


def stream_email(refnr: str, force: bool = False) -> Iterator[dict[str, Any]]:
    """Like ``ensure_email``, but yields events while the model writes.

    Events (one dict each, sent to the browser as one JSON line):

        {"type": "start", "model": "llama3"}
        {"type": "token", "text": "Sehr "}     repeated, one per piece of text
        {"type": "done", "job": {...}}         the final, cleaned, stored job

    If Ollama fails, even halfway through, or the text is unusable, ``done``
    carries the template email instead (``email_source == "template"``, the
    reason in ``email_note``). The browser always shows ``done.job`` as the
    final text, because cleaning may change what was streamed.

    This is a generator, so none of its code runs until someone iterates over
    it. app.py therefore checks job and profile *before* starting the stream,
    while it can still answer with a normal 404/400 error.
    """
    job = get_job(refnr)
    if job["application_email"] and not force:
        yield {"type": "done", "job": job}
        return
    profile = require_profile()
    # If the browser disconnects, Python raises GeneratorExit at the current
    # ``yield``; leaving the with-block then still releases the lock.
    with _generation_lock:
        yield {"type": "start", "model": emails.MODEL}
        parts: list[str] = []
        try:
            for chunk in emails.ollama_stream(job, profile):
                parts.append(chunk)
                yield {"type": "token", "text": chunk}
        except (requests.RequestException, ValueError) as exc:
            result = emails.result_from_error(job, profile, exc)
        else:
            # ``else`` after ``try`` runs only if no exception happened.
            result = emails.result_from_text(job, profile, "".join(parts))
        yield {"type": "done", "job": _store_email(refnr, *result)}


def _store_email(refnr: str, text: str, source: str, note: str | None) -> dict[str, Any]:
    """Save the email of one job; re-reads the file so no other change is lost."""
    with store.update() as state:
        job = store.find(state, refnr)
        job["application_email"] = text
        job["email_source"] = source
        job["email_note"] = note
    return job


def main() -> None:
    state = refresh_jobs()
    if "--emails" in sys.argv:  # sys.argv = the words typed after "python"
        for job in state["jobs"]:
            if job["status"] == "new" and not job["application_email"]:
                print(f"E-Mail für {job['title']} …", flush=True)
                ensure_email(job["refnr"])


# True only when this file is started directly ("python minijob.py"),
# not when another module imports it (as app.py does).
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting
        print(f"Fehler: {exc}", file=sys.stderr)
        sys.exit(1)  # a non-zero exit code tells the shell that it failed
