"""Local Flask server for the Minijob finder dashboard.

Serves the dashboard and a small JSON API:

    GET  /api/jobs                   stored jobs (jobs.json)
    POST /api/refresh                fetch listings for {location, radius} (fast)
    POST /api/jobs/<refnr>/email     generate the application email for one job
    POST /api/jobs/<refnr>/email/stream   same, streamed live as NDJSON
    POST /api/jobs/<refnr>/status    set status: new | applied | hidden
    GET  /api/profile                applicant profile used for the emails
    PUT  /api/profile                save the applicant profile
    GET  /api/ollama                 is the local model reachable?

How a click in the browser reaches the code:

    browser (static/index.html)
      -> fetch("/api/...")          HTTP request with JSON
      -> route function below       picks the right function
      -> minijob / store / emails   does the work
      <- jsonify(result)            HTTP response with JSON
    browser renders the new data

This file contains no logic of its own. It only translates between HTTP and
Python: request data in, function call, result or error as JSON out.

Errors are mapped to HTTP status codes the browser can check:
    400 Bad Request   invalid input (ValueError)
    404 Not Found     unknown job (KeyError)
    409 Conflict      a refresh is already running
    502 Bad Gateway   the job board could not be reached
"""

from __future__ import annotations

import json
import threading

import requests
from flask import Flask, jsonify, request, send_from_directory, stream_with_context

import emails
import minijob
import store

app = Flask(__name__, static_folder="static")

# Guards against two overlapping refreshes (e.g. a double click).
_refresh_lock = threading.Lock()


# ``@app.route(path)`` registers the function below it: when a request for
# that path arrives, Flask calls the function and sends back what it returns.
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/jobs")
def api_jobs():
    # jsonify turns the Python dict into a JSON response.
    return jsonify(store.load_state())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    # blocking=False: don't wait if the lock is taken, just report it.
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"error": "Eine Aktualisierung läuft bereits."}), 409
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(minijob.refresh_jobs(data.get("location"), data.get("radius"), verbose=True))
    except requests.RequestException as exc:
        return jsonify({"error": f"Jobbörse nicht erreichbar: {exc}"}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surface error to the UI
        return jsonify({"error": str(exc)}), 500
    finally:
        # ``finally`` always runs, even after return or an error, so the lock
        # is never left locked.
        _refresh_lock.release()


# ``<refnr>`` is a placeholder: Flask passes that part of the URL as argument.
@app.route("/api/jobs/<refnr>/email", methods=["POST"])
def api_email(refnr: str):
    # silent=True returns None instead of failing when the body is not JSON.
    force = bool((request.get_json(silent=True) or {}).get("force"))
    try:
        return jsonify(minijob.ensure_email(refnr, force=force))
    except KeyError:
        return jsonify({"error": "Stelle nicht gefunden."}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/jobs/<refnr>/email/stream", methods=["POST"])
def api_email_stream(refnr: str):
    """Stream the email as NDJSON: one JSON event per line (see minijob.stream_email).

    NDJSON ("newline-delimited JSON") lets the browser process each line as
    soon as it arrives, so the text appears word by word.
    """
    force = bool((request.get_json(silent=True) or {}).get("force"))
    # Check everything that can fail *before* the stream starts: once the
    # first line is sent, the status code (200) can no longer be changed.
    try:
        minijob.get_job(refnr)
        minijob.require_profile()
    except KeyError:
        return jsonify({"error": "Stelle nicht gefunden."}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    def events():
        for event in minijob.stream_email(refnr, force=force):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    # A generator as response body makes Flask send each yielded line right
    # away. stream_with_context keeps the request available while streaming.
    return app.response_class(
        stream_with_context(events()),
        mimetype="application/x-ndjson",
        # Tell browsers and proxies not to cache or hold back the lines.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/jobs/<refnr>/status", methods=["POST"])
def api_status(refnr: str):
    status = (request.get_json(silent=True) or {}).get("status", "")
    try:
        return jsonify(store.set_status(refnr, status))
    except KeyError:
        return jsonify({"error": "Stelle nicht gefunden."}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/profile")
def api_profile():
    return jsonify(store.load_profile())


@app.route("/api/profile", methods=["PUT"])
def api_save_profile():
    try:
        return jsonify(store.save_profile(request.get_json(silent=True)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ollama")
def api_ollama():
    return jsonify(emails.ollama_status())


if __name__ == "__main__":
    print("Minijob Finder läuft auf http://127.0.0.1:5000")
    # 127.0.0.1 = only reachable from this PC, not from the network.
    # threaded=True: each request runs in its own thread, so the page stays
    # usable while an email is being generated. This is why store.py and
    # minijob.py need locks.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
