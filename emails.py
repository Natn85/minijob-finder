"""Application emails: written by a local Ollama model, with a fixed template as fallback.

Flow of ``generate_email(job, profile)``:

1. Build a system prompt from the profile (who is applying, the rules).
2. Send it together with the job (title, company, description) to Ollama.
3. Clean up the answer (code fences, subject lines, missing signature).
4. If anything goes wrong (Ollama not running, too slow, useless answer),
   return the template email instead, with a note explaining why.

The caller therefore always gets a usable email.

``ollama_stream`` is the live variant: it yields the text piece by piece
while the model writes. ``minijob.stream_email`` passes those pieces to the
browser and finishes with ``result_from_text`` / ``result_from_error``, the
same steps 3 and 4 as above.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from typing import Any

import requests
from dotenv import load_dotenv

# Reads a ``.env`` file (if present) into the environment variables, so the
# settings below can be changed without touching the code.
load_dotenv()

# Ollama (https://ollama.com): start it with ``ollama serve`` and pull a model,
# e.g. ``ollama pull qwen2.5:7b``. Override via OLLAMA_MODEL / OLLAMA_HOST.
MODEL = os.getenv("OLLAMA_MODEL", "llama3")  # second argument = default value
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_CHAT_URL = f"{OLLAMA_HOST}/api/chat"
# The first request after starting Ollama also loads the model into memory,
# which can take a few minutes on a CPU.
OLLAMA_TIMEOUT = 300

# Profile fields as they are described to the model.
PROFILE_LABELS = {
    "name": "Name",
    "city": "Wohnort",
    "occupation": "Macht derzeit",
    "skills": "Kenntnisse",
    "availability": "Verfügbar",
    "about": "Weitere Infos",
}

# The system prompt: tells the model who it writes for and which rules apply.
# ``{name}`` and ``{profile}`` are filled in by ``build_system_prompt``.
_PROMPT = """Du hilfst {name} dabei, kurze, maßgeschneiderte Bewerbungs-E-Mails für Minijobs zu verfassen.

Profil des Bewerbers:
{profile}

Aufgabe:
Schreibe für die unten genannte Stelle eine kurze deutsche Bewerbungs-E-Mail.

Regeln:
- Sprache: Deutsch. Länge: maximal ca. 130 Wörter.
- Tonfall: kurz, direkt, höflich, nicht steif.
- Beginne mit "Sehr geehrte Damen und Herren," und danach mit "hiermit bewerbe ich mich auf Ihre ausgeschriebene Stelle als ...".
- Beziehe dich konkret auf die Stelle und das Unternehmen.
- Erwähne Kenntnisse nur, wenn sie zur Stelle passen.
- Betone die Suche nach einem zuverlässigen Minijob.
- Erfinde nichts: keine Berufserfahrung, Zertifikate, Führerscheine oder Verfügbarkeiten, die nicht im Profil stehen.
- Keine Platzhalter in eckigen Klammern.
- Schließe mit "Mit freundlichen Grüßen" und dem Namen {name}.
- Gib NUR den E-Mail-Text aus, ohne Betreffzeile, ohne Erklärungen, ohne Markdown."""


def build_system_prompt(profile: dict[str, str]) -> str:
    """Insert the profile into the prompt, one line per filled-in field.

    Empty fields are left out completely, so the model has nothing to
    misread or invent from.
    """
    lines = [f"- {label}: {profile[key]}" for key, label in PROFILE_LABELS.items() if profile.get(key)]
    return _PROMPT.format(name=profile["name"], profile="\n".join(lines))


# Matches gender tags such as "(m/w/d)", "(w/m/d)" or a bare "m/w/d".
_GENDER_TAG = re.compile(
    r"\s*\??\s*\(\s*[mwdfi*]\s*[/|]\s*[mwdfi*]\s*(?:[/|]\s*[mwdfi*]\s*)?\)\s*\??"  # (m/w/d), ? (w/m/d) ?
    r"|\s+\b[mwd]\s*/\s*[mwd]\s*/\s*[mwd]\b",                                      # bare m/w/d
    re.I,
)


# --- Subject and template ---------------------------------------------------


def clean_title(title: str | None) -> str:
    """Strip the gender tag, a leading/trailing "Minijob" and a trailing location.

    Words in the middle of the title are never removed ("Minijob-Basis" stays).

    Example: "Minijob Verkäufer (m/w/d) in 97070 Würzburg" -> "Verkäufer"
    """
    original = re.sub(r"\s+", " ", (title or "")).strip()
    t = _GENDER_TAG.sub(" ", original)
    t = re.sub(r"^\s*minijob\s+", "", t, flags=re.I)         # ^ = start of text
    t = re.sub(r"[\s,/–-]+minijob\s*$", "", t, flags=re.I)  # $ = end of text
    t = re.sub(r"\s+(?:in\s+)?(?:\d{5}\s+)?Würzburg\s*$", "", t)
    t = re.sub(r"\s+\d{5}\s*$", "", t)                       # trailing postcode
    t = re.sub(r"\s+", " ", t).strip(" -–—:,/|")
    return t or original or "Ihre Stellenanzeige"


def build_subject(job: dict[str, Any]) -> str:
    title = clean_title(job.get("title"))
    return f"Bewerbung: {title}" if "minijob" in title.lower() else f"Bewerbung: {title} (Minijob)"


def build_template_email(job: dict[str, Any], profile: dict[str, str]) -> str:
    """Fixed email used when Ollama is not available.

    The title is quoted rather than used in "Stelle als ...", because many
    listing titles are whole phrases that would break the sentence.
    """
    title = clean_title(job.get("title"))
    company = (job.get("company") or "").strip()
    at_company = f" bei {company}" if company and "Unbekannt" not in company else ""
    name = profile["name"]

    # Each optional profile field adds one sentence, so empty fields never
    # leave gaps like "ich wohne in ." in the text.
    about_me = f"Mein Name ist {name}"
    about_me += f", ich wohne in {profile['city']}." if profile.get("city") else "."
    if profile.get("occupation"):
        about_me += f" Derzeit mache ich {profile['occupation']}."
    if profile.get("availability"):
        about_me += f" Ich suche einen zuverlässigen Minijob und bin {profile['availability']} verfügbar."
    else:
        about_me += " Ich suche einen zuverlässigen Minijob."

    # Strings next to each other inside parentheses are joined into one.
    return (
        "Sehr geehrte Damen und Herren,\n\n"
        f"hiermit bewerbe ich mich auf Ihre Stellenanzeige „{title}“{at_company}. {about_me}\n\n"
        "Ich arbeite sorgfältig, bin pünktlich und lerne mich schnell in neue Aufgaben ein. "
        "Über eine Rückmeldung und die Möglichkeit, mich persönlich vorzustellen, freue ich mich sehr.\n\n"
        f"Mit freundlichen Grüßen\n{name}"
    )


# --- Ollama -----------------------------------------------------------------


def ollama_status() -> dict[str, Any]:
    """Whether Ollama is running and the configured model is installed.

    Uses a short timeout because the dashboard calls it on every page load.
    """
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=1.5)  # lists installed models
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError):
        return {"up": False, "model": MODEL, "model_installed": False}
    # "llama3" should also match "llama3:latest".
    installed = any(n == MODEL or n.startswith(MODEL + ":") for n in names)
    return {"up": True, "model": MODEL, "model_installed": installed}


# Result of every generation: (email text, "ollama" or "template", note).
# The note says why the template was used, so the dashboard can show it.
EmailResult = tuple[str, str, str | None]


def generate_email(job: dict[str, Any], profile: dict[str, str]) -> EmailResult:
    """Write the email in one request and return ``(text, source, note)``.

    ``source`` is ``"ollama"`` or ``"template"``; ``note`` explains why the
    template was used so the dashboard can show it.
    """
    try:
        raw = _ollama_email(job, profile)
    except (requests.RequestException, ValueError) as exc:
        return result_from_error(job, profile, exc)
    return result_from_text(job, profile, raw)


def result_from_text(job: dict[str, Any], profile: dict[str, str], raw: str) -> EmailResult:
    """Clean the model's answer; use the template if it is not usable."""
    text = clean_email(raw, profile["name"])
    if not _usable(text):
        return build_template_email(job, profile), "template", "Die Antwort des Modells war unbrauchbar. Nochmal versuchen."
    return text, "ollama", None


def result_from_error(job: dict[str, Any], profile: dict[str, str], exc: Exception) -> EmailResult:
    """The template email, with the reason why Ollama could not be used."""
    return build_template_email(job, profile), "template", _fallback_note(exc)


def ollama_stream(job: dict[str, Any], profile: dict[str, str]) -> Iterator[str]:
    """Yield the email text piece by piece as Ollama produces it.

    This is a generator: each ``yield`` hands one piece to the caller right
    away, while the model is still writing the rest.

    With ``stream: true`` Ollama answers with one JSON object per line
    (NDJSON), e.g. ``{"message": {"content": "Sehr "}, "done": false}``.
    """
    # timeout=(connect, read): connecting must be quick; the read timeout
    # applies to each piece, and the first one can take long while the model
    # loads into memory.
    with requests.post(
        OLLAMA_CHAT_URL, json=_ollama_payload(job, profile, stream=True),
        stream=True, timeout=(5, OLLAMA_TIMEOUT),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            if data.get("error"):
                raise ValueError(data["error"])
            chunk = (data.get("message") or {}).get("content") or ""
            if chunk:
                yield chunk
            if data.get("done"):
                break


def clean_email(text: str, name: str) -> str:
    """Remove code fences, subject lines and markdown that models tend to add."""
    text = (text or "").strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    lines = [ln for ln in text.splitlines() if not re.match(r"\s*(Betreff|Subject)\s*:", ln, re.I)]
    text = "\n".join(lines).replace("**", "").strip()
    # Make sure the email is signed with the applicant's name.
    if name not in text:
        if "Mit freundlichen" in text:
            text += f"\n{name}"
        else:
            text += f"\n\nMit freundlichen Grüßen\n{name}"
    return text


def _ollama_payload(job: dict[str, Any], profile: dict[str, str], stream: bool) -> dict[str, Any]:
    """The chat request for Ollama.

    A chat request consists of messages with roles:
    "system" sets the rules and the profile, "user" contains the actual job.
    """
    description = job.get("description") or "(Keine Stellenbeschreibung verfügbar.)"
    # Long descriptions are cut off: small models get slower and less focused
    # the more text they have to read.
    if len(description) > 3000:
        description = description[:3000] + "…"
    user_content = (
        f"Stellentitel: {clean_title(job.get('title'))}\n"
        f"Unternehmen: {job.get('company')}\n"
        f"Ort: {job.get('location')}\n\n"
        f"Stellenbeschreibung:\n{description}"
    )
    return {
        "model": MODEL,
        "stream": stream,  # True: answer piece by piece; False: wait for all of it
        # Temperature controls randomness: 0 = always the same wording,
        # higher = more varied. 0.4 keeps it natural but not wild.
        "options": {"temperature": 0.4},
        "messages": [
            {"role": "system", "content": build_system_prompt(profile)},
            {"role": "user", "content": user_content},
        ],
    }


def _ollama_email(job: dict[str, Any], profile: dict[str, str]) -> str:
    """Send one chat request and return the complete answer text."""
    resp = requests.post(OLLAMA_CHAT_URL, json=_ollama_payload(job, profile, stream=False), timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    return (resp.json().get("message", {}).get("content") or "").strip()


def _fallback_note(exc: Exception) -> str:
    """Human-readable reason why the template had to be used.

    The checks go from specific to general: ConnectionError, Timeout and
    HTTPError are all subclasses of RequestException.
    """
    if isinstance(exc, requests.ConnectionError):
        return "Ollama läuft nicht (starte es mit 'ollama serve')."
    if isinstance(exc, requests.Timeout):
        return "Ollama hat zu lange gebraucht. Nochmal versuchen, das Modell ist jetzt geladen."
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 404:
        return f"Modell '{MODEL}' ist nicht installiert (ollama pull {MODEL})."
    return f"Ollama-Fehler: {exc}"


def _usable(text: str) -> bool:
    """Too short or containing placeholders like "[Name]": not usable."""
    return len(text) >= 80 and not re.search(r"\[[^\]]+\]", text)
