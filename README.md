# I don't want to play Krillion

A small, read-only Flask site showing the current daily prompts, their published
100-point Krillion answers, and the game's explanations. Plain HTML and inline
CSS: no JavaScript, Node, build step, browser extension, account, or API key.

## Run locally

Requires Python 3.10 or newer. In a terminal:

```bash
cd /home/yee/Documents/I-dont-want-to-play-krillion
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. Stop with Ctrl+C. Next time, just activate the
environment and run `python app.py`. Set `PORT=5001` if port 5000 is occupied.
Refresh the page to see a new day's answers.

## Deploy on Vercel

Push this repository to your Git provider and import it into Vercel. Vercel
detects Flask from `app.py` and installs `requirements.txt`. Leave the build and
output directory overrides unset. No environment variables or database service
are required. Alternatively, run `vercel` from this folder if you use its CLI.

`vercel.json` gives the first request up to 60 seconds to fetch the answer sheet.
Both ignore files exclude `example_code.js` and local cache/environment files.
The example remains on your machine and is never part of the application.

Official reference: [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask).

## Deploy on PythonAnywhere

1. Upload or clone the repository, then create a Python 3.10+ virtual environment
   and install `requirements.txt` as above.
2. In the Web tab, add a web app with **Manual configuration**. Select the same
   Python version as your environment and enter the environment's full path.
3. Edit the WSGI configuration to contain the following, replacing the path with
   the location of your checkout:

   ```python
   import sys
   sys.path.insert(0, '/home/YOUR_USERNAME/I-dont-want-to-play-krillion')
   from wsgi import application
   ```

4. Reload the web app. No static-file mapping is needed.

The host must permit outbound HTTPS to `krillion.io`. PythonAnywhere free
accounts restrict outbound access; check that the domain is allowed or use a
plan/host with unrestricted outbound HTTPS. See
[PythonAnywhere's outbound access rules](https://help.pythonanywhere.com/pages/403ForbiddenError/).

Official reference: [existing Flask apps on PythonAnywhere](https://help.pythonanywhere.com/pages/Flask/).

Other Python servers can serve the entire application using:

```bash
.venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 app:app
```

## API and caching

The adapter was written from scratch. `example_code.js` was inspected only to
identify the endpoint/schema, then the actual responses were verified against
the live [Krillion site](https://krillion.io/). No userscript code is included.

- `GET https://krillion.io/api/today` supplies `date`, `resetsAt`, and ordered
  `prompts` containing `id` and `text`.
- `GET https://krillion.io/api/reveal?date=YYYY-MM-DD` supplies the matching
  `date` and `prompts`, each with `id`, `text`, and `answers`.
- The site displays answers with **both** `tier: "krillion"` and `score: 100`.
  `answer` is the displayed answer; `quip` is its published explanation. All
  Krillion picks are shown if a prompt has more than one. Missing answers or
  explanations are stated explicitly; nothing is invented or replaced by a
  lower-tier answer.

The first visit fetches the two endpoints. The answer sheet was about 33 MB when
verified, so only the small normalized set of displayed answers is stored.
Subsequent requests use SQLite until the upstream `resetsAt` timestamp, including
across Flask restarts and WSGI workers sharing that file. An SQLite transaction
serializes concurrent refreshes. Failed refreshes have a 60-second cooldown.
Yesterday's answers are never served after their reset; an upstream outage
produces a small HTTP 503 page instead.

Locally and on persistent Python hosts, the cache lives in
`.cache/answers.sqlite3`. Set `KRILLION_CACHE_DIR` to an absolute writable folder
to put it elsewhere. To force a refresh for an editorial correction, stop the
server, remove this disposable cache file, and restart it.

On Vercel, SQLite lives in `/tmp/krillion` and is **per instance and temporary**.
The HTML also sends `s-maxage` capped at the daily reset (with a five-second
margin), allowing Vercel's CDN to reuse the rendered sheet. Separate cold
instances, regions, cache misses, and deployments can each fetch again; this is
not a globally shared database or a guarantee of exactly two calls per day.
There are no polling tasks or client-to-Krillion requests. For the strongest
cache sharing without extra services, run Flask on a persistent Python host.

CDN reference: [Vercel cache headers](https://vercel.com/docs/caching/cache-control-headers).

## Checks

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use fictional API fixtures and temporary caches, with no live API calls.
They cover top-tier selection, schema mismatches, reset expiry, concurrent cache
access, failure cooldown, HTML escaping, and response cache headers.
