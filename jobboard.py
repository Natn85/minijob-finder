"""Client for the job board of the Bundesagentur für Arbeit.

Searches Minijob listings and turns them into the job shape used by the rest
of the app. Uses the same public API as the arbeitsagentur.de job search.

This is the only module that knows how the Arbeitsagentur API looks. Every
other module works with our own job dictionaries, so when the API changes
(new version, renamed fields), only this file needs to be updated.

A job goes through two steps here:

1. ``search()`` returns short "listings": refnr, title, company, location, URL.
2. ``with_details()`` adds the description and a contact email. It needs one
   extra request per job, so the caller only runs it for jobs it has not
   seen before.
"""

# Lets us write modern type hints like ``str | None`` and evaluates them lazily.
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import requests

# --- Configuration ----------------------------------------------------------

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc"
SEARCH_URL = f"{API_BASE}/v6/jobs"
# ``{{}}`` in an f-string becomes a literal ``{}``, which ``.format()`` fills later.
DETAIL_URL = f"{API_BASE}/v4/jobdetails/{{}}"  # expects the base64-encoded refnr
API_HEADERS = {"X-API-Key": "jobboerse-jobsuche"}  # public key of the job board
REQUEST_TIMEOUT = 30  # seconds; without a timeout a hanging server blocks forever

# Sent as the query string: ?wo=Würzburg&arbeitszeit=mj&umkreis=25&...
SEARCH_PARAMS = {
    "wo": "Würzburg",
    "arbeitszeit": "mj",   # mj = Minijob
    "umkreis": "25",       # radius in km
    "angebotsart": "1",    # 1 = regular job offers
    "size": "50",          # results per page
}
MAX_PAGES = 2              # at most MAX_PAGES * size listings per refresh

# Titles containing one of these need a professional qualification and are
# always skipped. Matching is case-insensitive and on substrings, so
# "Therapeut" also covers Ergo-, Physio- and Lymphtherapeut.
QUALIFICATION_KEYWORDS = [
    "Pflegefachkraft", "Pflegefachfrau", "Pflegefachmann", "Krankenpfleger",
    "Heilerziehungspfleger", "Erzieher", "Lehrkraft", "Ausbilder",
    "Therapeut", "Masseur", "34a",  # § 34a GewO: certified security staff
]
# Titles containing one of these need a university enrollment. They are kept
# if the title also says "Minijob" or "Aushilfe".
ENROLLMENT_KEYWORDS = ["Werkstudent", "Student", "Studierende"]

# Regular expressions are compiled once here instead of on every call.
#
# Wording that marks a listing as a Minijob, e.g. "Minijob-Basis",
# "geringfügig beschäftigt", "Aushilfe" or "auf 603 €-Basis".
#   mini[\s-]?job   "minijob", "mini job" or "mini-job"
#   \d{3}           three digits, e.g. the 603 in "603 €-Basis"
#   (?:€|euro)?     optional currency; (?:...) groups without capturing
#   re.I            ignore upper/lower case
MINIJOB_WORDING = re.compile(
    r"mini[\s-]?job|geringfügig|aushilfe|\d{3}\s*-?\s*(?:€|euro)?\s*-?\s*basis",
    re.I,
)

# Rough email pattern: characters, "@", then domain parts joined by single
# dots. A sentence-ending "." is therefore not part of the match:
# "info@fewue.de." -> "info@fewue.de".
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# --- Public functions -------------------------------------------------------


def search() -> list[dict[str, Any]]:
    """Return the listings of up to MAX_PAGES result pages, de-duplicated.

    The API returns results in pages. We request page 1, 2, ... and stop when
    MAX_PAGES is reached or a page comes back with fewer than ``size`` entries,
    which means it was the last one.
    """
    size = int(SEARCH_PARAMS["size"])
    # A dict keyed by refnr removes duplicates and keeps the original order.
    listings: dict[str, dict[str, Any]] = {}
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = requests.get(
                SEARCH_URL,
                headers=API_HEADERS,
                # ``{**a, "k": v}`` copies dict ``a`` and adds or overrides one key.
                params={**SEARCH_PARAMS, "page": str(page)},
                timeout=REQUEST_TIMEOUT,
            )
            # Turns HTTP errors (403, 500, ...) into a Python exception.
            resp.raise_for_status()
            batch = resp.json().get("ergebnisliste") or []
        except (requests.RequestException, ValueError):
            # ValueError is raised by ``resp.json()`` when the body is not JSON.
            if page == 1:
                raise  # nothing loaded at all: let the caller show the error
            break  # a missing follow-up page is not an error
        for raw in batch:
            listing = _to_listing(raw)
            # Only stores the listing if this refnr is not in the dict yet.
            listings.setdefault(listing["refnr"], listing)
        if len(batch) < size:
            break
    return list(listings.values())


def is_excluded(title: str) -> bool:
    """Whether a listing should be skipped based on its title."""
    t = title.lower()
    # ``any(...)`` is True as soon as one keyword is found in the title.
    if any(k.lower() in t for k in QUALIFICATION_KEYWORDS):
        return True
    if "minijob" in t or "aushilfe" in t:
        return False
    return any(k.lower() in t for k in ENROLLMENT_KEYWORDS)


def mentions_minijob(job: dict[str, Any]) -> bool:
    """Whether title or description call the job a Minijob.

    The job board's Minijob filter relies on a flag set by the employer, which
    is also set on regular jobs that merely allow a Minijob.
    """
    return bool(MINIJOB_WORDING.search(f"{job.get('title', '')} {job.get('description', '')}"))


def with_details(listing: dict[str, Any]) -> dict[str, Any]:
    """Add description and contact email from the listing's detail page.

    Runs in several threads at once (see ``minijob.refresh_jobs``), which is
    safe because it only reads its input and returns a new dict.
    """
    detail = _fetch_detail(listing["refnr"])
    description = (detail.get("stellenangebotsBeschreibung") or "").strip()
    return {
        **listing,
        "description": description,
        "contact_email": _extract_email(detail, description),
    }


# --- Internal helpers -------------------------------------------------------
# A leading underscore marks a function as internal to this module.


def _to_listing(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate one raw API result (German field names) into our job shape."""
    # ``a or b`` picks the first value that is not empty or None.
    title = raw.get("stellenangebotsTitel") or raw.get("hauptberuf") or "Unbekannte Stelle"
    company = raw.get("firma") or "Unbekanntes Unternehmen"
    refnr = raw.get("referenznummer")
    locations = raw.get("stellenlokationen") or [{}]

    if refnr:
        application_url = raw.get("externeURL") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
    else:
        # Every job needs a stable ID. Without a refnr we derive one from title
        # and company: the same input always gives the same hash, so the job
        # is recognized again on the next refresh.
        refnr = "x-" + hashlib.sha1(f"{title}|{company}".encode()).hexdigest()[:12]
        application_url = raw.get("externeURL") or ""

    return {
        "refnr": refnr,
        "title": title,
        "company": company,
        "location": _format_location(locations[0].get("adresse")),
        "application_url": application_url,
    }


def _format_location(address: dict[str, Any] | None) -> str:
    """Build e.g. "97074 Würzburg (Bayern)" from the address fields."""
    if not address:
        return "Unbekannt"
    location = " ".join(p for p in (address.get("plz"), address.get("ort")) if p)
    region = (address.get("region") or "").title()  # "BAYERN" -> "Bayern"
    if region and region not in location:
        location = f"{location} ({region})" if location else region
    return location or "Unbekannt"


def _fetch_detail(refnr: str) -> dict[str, Any]:
    """Detail record of one listing, or ``{}`` if it cannot be loaded.

    Details are optional extras, so a failure here must not stop the refresh:
    the job is simply saved without description.
    """
    if refnr.startswith("x-"):
        return {}  # self-made ID, the API does not know it
    # The detail endpoint expects the refnr base64-encoded in the URL,
    # e.g. "10001-1003758547-S" -> "MTAwMDEtMTAwMzc1ODU0Ny1T".
    encoded = base64.b64encode(refnr.encode()).decode()
    try:
        resp = requests.get(DETAIL_URL.format(encoded), headers=API_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return {}


def _extract_email(detail: dict[str, Any], description: str) -> str | None:
    """Contact email from the detail record, else the first one in the description."""
    contact = detail.get("kontaktDaten") or {}
    if isinstance(contact, dict):
        email = contact.get("email") or contact.get("eMail")
        if email:
            return email
    match = EMAIL_RE.search(description)
    return match.group(0) if match else None
