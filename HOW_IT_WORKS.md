# How the Minijob Finder works

This guide explains how the parts of the app work together. The code itself
has comments on the details; this is the big picture.

## The parts

```
 Browser                     Python (Flask server)                 Outside world
┌──────────────────┐        ┌──────────────────────────────┐
│ static/index.html│ HTTP   │ app.py      routes, no logic │
│  - state         │ ─────► │   │                          │
│  - render()      │ ◄───── │   ▼                          │
│  - api()/post()  │  JSON  │ minijob.py  the workflows    │
└──────────────────┘        │   │      │        │          │
                            │   ▼      ▼        ▼          │
                            │ jobboard emails  store       │
                            └────┬───────┬────────┬────────┘
                                 │       │        │
                                 ▼       ▼        ▼
                       Arbeitsagentur  Ollama   jobs.json
                            API       (local)   profile.json
```

| File | Job | Talks to |
|---|---|---|
| `static/index.html` | Shows the jobs, sends clicks to the server | `app.py` over HTTP |
| `app.py` | Translates HTTP requests into function calls and back | `minijob`, `store`, `emails` |
| `minijob.py` | The workflows: refresh jobs, write an email | `jobboard`, `emails`, `store` |
| `jobboard.py` | Everything about the Arbeitsagentur API | the internet |
| `emails.py` | Prompt, Ollama call, template fallback | Ollama on your PC |
| `store.py` | Reading and writing the JSON files | the disk |
| `tests/` | Checks all of the above with a fake job board and a fake Ollama | nothing outside |

Each module has one reason to change. When the Arbeitsagentur changes its
API, you only touch `jobboard.py`. When you want different emails, only
`emails.py`. This idea is called *separation of concerns*.

## Flow 1: Opening the page

1. The browser asks for `/`, and `app.py` sends `static/index.html`.
2. `init()` in the page sends three requests:
   - `GET /api/profile` fills the profile form
   - `GET /api/jobs` loads `jobs.json` through `store.load_state()`
   - `GET /api/ollama` checks whether Ollama is running
3. Each answer is stored in a variable, and `render()` draws the page.

Nothing is fetched from the Arbeitsagentur here, which is why the page opens
instantly.

## Flow 2: Clicking "Jobs aktualisieren"

```
index.html          app.py              minijob.refresh_jobs()
refreshJobs() ───► POST /api/refresh ─► 1. jobboard.search()         ─► API: search, 2 pages
                                        2. jobboard.is_excluded()       drop unsuitable titles
                                        3. store.load_state()           which jobs are known?
                                        4. jobboard.with_details()   ─► API: one request per NEW job
                                           (8 at once in threads)
                                        5. store.update()               merge and save jobs.json
state = answer ◄── JSON of all jobs ◄──
render()
```

The important trick is step 3 and 4: details are only loaded for jobs that
are not in `jobs.json` yet. The first refresh makes ~90 detail requests,
every later one only a handful. That is a simple form of *caching*.

The merge in step 5 keeps your work: a job you marked "Beworben" stays even
when the listing disappears from the job board, and statuses are never reset.

## Flow 3: Clicking "E-Mail generieren"

The email is *streamed*: the browser shows each word as soon as the model has
written it, instead of waiting a minute for the whole text.

```
index.html               app.py                               minijob.stream_email()
generate(id) ──► POST /api/jobs/<id>/email/stream
                 checks job (404) and profile name (400)
                 then streams one JSON line per event ──►  yield {"type": "start"}
                                                           emails.ollama_stream(job, profile)
                                                             └─ Ollama sends the text piece by piece
◄── {"type": "token", "text": "Sehr "} ◄─────────────────  yield {"type": "token", ...}   (repeated)
renderStream(id): append text, move cursor                 emails.result_from_text()   clean + check
                                                           or result_from_error()      template + reason
                                                           _store_email()               save in jobs.json
◄── {"type": "done", "job": {...}} ◄─────────────────────  yield {"type": "done", ...}
finish(id): show the final text, "Fertig in … s"
```

Three ideas make this work:

- **Generators.** `stream_email` and `ollama_stream` use `yield`: they hand out
  one piece at a time and pause until the next piece is asked for. Flask sends
  each piece to the browser right away.
- **NDJSON.** One JSON object per line. The browser reads the response as it
  arrives (`res.body.getReader()`), splits it at line breaks and handles each
  event.
- **Graceful degradation.** If Ollama is off, too slow or fails halfway, the
  `done` event carries the template email and a note explaining why. The page
  shows that note in yellow. The app gets worse, but it never breaks.

`POST /api/jobs/<id>/email` (`minijob.ensure_email`) does the same in one
piece, without streaming. The command line (`python minijob.py --emails`) and
older pages use it.

## Flow 4: Saving the profile

`PUT /api/profile` → `store.save_profile()` checks the data (only known
fields, text only, length limit, name required) and writes `profile.json`.
The next email reads the new profile. Emails that already exist are not
changed until you click "Neu schreiben".

## What one job looks like

Every function passes jobs around as plain dictionaries with these keys:

```json
{
  "refnr": "12811-2369400-S",
  "title": "Minijob / Aushilfe im Front Office (m/w/d)",
  "company": "COMCAVE.GROUP®",
  "location": "97074 Würzburg (Bayern)",
  "application_url": "https://...",
  "description": "Gestalte mit uns ...",
  "contact_email": null,
  "status": "new",
  "application_email": "",
  "email_source": null,
  "email_note": null,
  "application_subject": "Bewerbung: Aushilfe im Front Office (Minijob)",
  "display_title": "Aushilfe im Front Office",
  "minijob_mentioned": true
}
```

- `refnr` is the ID. Everything (status, email, the URLs `/api/jobs/<refnr>/…`)
  is linked through it.
- The first five fields come from `jobboard._to_listing`, the next two from
  `jobboard.with_details`.
- `status`, `application_email`, `email_source` and `email_note` are your data
  and survive refreshes.
- `application_subject`, `display_title` and `minijob_mentioned` are *derived*:
  `store.normalize` recomputes them on every load, so they can never be outdated.

## Concepts worth knowing

Where to find each one in the code:

| Concept | What it means | Where |
|---|---|---|
| REST API | Programs talk over HTTP: a method (GET/POST/PUT), a path, JSON data | `app.py`, `jobboard.py` |
| HTTP status codes | 200 OK, 400 bad input, 404 not found, 409 conflict, 502 upstream down | `app.py` |
| Exceptions as signals | `KeyError` means "not found", `ValueError` means "bad input"; `app.py` maps them to 404/400 | `store.py`, `app.py` |
| Threads | Several things at once. Used for parallel downloads and for parallel browser requests | `minijob.py`, `app.py` |
| Locks | Only one thread at a time may change `jobs.json`, otherwise changes get lost (*race condition*) | `store.py` |
| Context managers | `with ...:` runs setup and cleanup code around a block, e.g. lock + load + save | `store.update()` |
| Atomic writes | Write to a temp file, then swap it in, so a crash never leaves half a file | `store._write_json` |
| Regular expressions | Patterns for finding text: emails, "(m/w/d)", "603 €-Basis" | `jobboard.py`, `emails.py` |
| Prompting an LLM | A system message sets rules and context, a user message has the task | `emails.py` |
| Input validation | Never trust data from the browser: check type, fields and length | `store.save_profile` |
| XSS protection | Text from outside is escaped before it goes into the HTML | `esc` in `index.html` |
| Render from state | The page is redrawn from one data object instead of patched by hand | `render()` in `index.html` |
| Environment config | Settings like the model name come from `.env`, not from the code | `emails.py` |
| Generators and streaming | `yield` hands out results one at a time; used to send the email word by word | `minijob.stream_email`, `emails.ollama_stream` |
| Automated tests | Code that checks the code. Fakes replace the internet and Ollama, so tests are fast and repeatable | `tests/` |
| Monkeypatching | A test temporarily swaps a function or file path for a fake one | `tests/conftest.py` |

## Things to try

Small changes that are good for learning how the pieces connect:

1. **Another city.** Change `"wo"` in `SEARCH_PARAMS` (`jobboard.py`), run
   `python minijob.py` and look at `jobs.json`.
2. **Watch the API.** Open `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs?wo=Würzburg&arbeitszeit=mj`
   in a tool like Postman or `curl` with the header `X-API-Key: jobboerse-jobsuche`
   and compare the fields with `_to_listing`.
3. **Change the prompt.** Edit a rule in `_PROMPT` (`emails.py`), click
   "Neu schreiben" on a job and compare the result.
4. **Break something on purpose.** Stop Ollama and write an email: you will
   see the template and the yellow note. That is the fallback path in
   `generate_email`.
5. **Use the API directly.** With the server running, open
   `http://127.0.0.1:5000/api/jobs` in the browser: that is exactly the JSON
   the page works with.
