"""A read-only daily Krillion answer sheet. Run with `python app.py`."""

import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import date, datetime

from flask import Flask, make_response, render_template, send_from_directory


API = "https://krillion.io/api/"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RETRY_SECONDS = 60
app = Flask(__name__, static_folder=None)


class AnswersUnavailable(Exception):
    """The upstream answer sheet is unavailable or has an unexpected schema."""


def fetch_json(endpoint):
    request = Request(API + endpoint, headers={
        "Accept": "application/json",
        "User-Agent": "KrillionDailyReader/1.0",
    })
    with urlopen(request, timeout=15) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise AnswersUnavailable("Upstream response exceeds the size limit")
    return json.loads(body)


def normalize(today, reveal, now):
    """Join by prompt identity; retain only published 100-point Krillion picks."""
    try:
        puzzle_date = date.fromisoformat(today["date"]).isoformat()
        reset = datetime.fromisoformat(today["resetsAt"].replace("Z", "+00:00"))
        if reset.tzinfo is None:
            raise ValueError("Reset has no timezone")
        expires_at = reset.timestamp()
        if not now < expires_at <= now + 26 * 60 * 60:
            raise ValueError("Reset is outside the current daily window")
        if reveal["date"] != puzzle_date:
            raise ValueError("Answer date does not match the puzzle")
        prompts, published = today["prompts"], reveal["prompts"]
        if not isinstance(prompts, list) or not prompts or not isinstance(published, list):
            raise ValueError("Missing prompts")
        by_id = {prompt["id"]: prompt for prompt in published}
        if len(by_id) != len(published):
            raise ValueError("Duplicate published prompt IDs")
        questions = []
        seen = set()
        for number, prompt in enumerate(prompts, 1):
            identity, text = prompt["id"], prompt["text"]
            if not isinstance(identity, str) or identity in seen:
                raise ValueError("Invalid prompt ID")
            seen.add(identity)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Missing prompt text")
            sheet = by_id[identity]
            if sheet["text"] != text or not isinstance(sheet["answers"], list):
                raise ValueError("Answer sheet does not match the prompt")
            answers = []
            for item in sheet["answers"]:
                if item.get("tier") != "krillion" or item.get("score") != 100:
                    continue
                answer, explanation = item["answer"], item.get("quip")
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("Missing Krillion answer")
                if explanation is not None and not isinstance(explanation, str):
                    raise ValueError("Invalid explanation")
                answers.append({
                    "answer": answer,
                    "explanation": (explanation or "").strip() or "No explanation published.",
                })
            questions.append({"number": number, "prompt": text, "answers": answers})
        return {"date": puzzle_date, "expires_at": expires_at, "questions": questions}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise AnswersUnavailable("The daily API schema changed or the puzzle rolled over") from error


def load_daily():
    today = fetch_json("today")
    try:
        puzzle_date = date.fromisoformat(today["date"]).isoformat()
    except (KeyError, TypeError, ValueError) as error:
        raise AnswersUnavailable("Invalid puzzle date") from error
    reveal = fetch_json("reveal?" + urlencode({"date": puzzle_date}))
    return normalize(today, reveal, time.time())


def cache_path():
    default = Path("/tmp/krillion") if os.environ.get("VERCEL") else Path(__file__).parent / ".cache"
    return Path(os.environ.get("KRILLION_CACHE_DIR", default)) / "answers.sqlite3"


def cached_daily(path=None, loader=None, clock=None):
    """SQLite serializes refreshes across threads and local WSGI workers."""
    path = Path(path) if path is not None else cache_path()
    loader, clock = loader or load_daily, clock or time.time
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=40)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS daily (id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT, retry_at REAL NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload, retry_at FROM daily WHERE id = 1").fetchone()
        now = clock()
        if row:
            saved = json.loads(row[0]) if row[0] else None
            if saved and now < saved["expires_at"]:
                connection.commit()
                return saved
            if now < row[1]:
                raise AnswersUnavailable("Waiting before retrying the daily API")
        try:
            fresh = loader()
            if fresh["expires_at"] <= clock():
                raise AnswersUnavailable("Puzzle expired during refresh")
        except (AnswersUnavailable, URLError, OSError, ValueError) as error:
            connection.execute("INSERT OR REPLACE INTO daily VALUES (1, NULL, ?)", (clock() + RETRY_SECONDS,))
            connection.commit()
            raise AnswersUnavailable("Could not refresh the daily answer sheet") from error
        connection.execute("INSERT OR REPLACE INTO daily VALUES (1, ?, 0)", (json.dumps(fresh),))
        connection.commit()
        return fresh
    finally:
        connection.close()


@app.get("/favicon.png")
def favicon():
    return send_from_directory(Path(app.root_path) / "public", "favicon.png")


@app.get("/")
def index():
    try:
        daily = cached_daily()
        response = make_response(render_template("index.html", questions=daily["questions"]))
        # A safety margin keeps a CDN response from crossing the upstream reset.
        ttl = max(0, math.floor(daily["expires_at"] - time.time()) - 5)
        response.headers["Cache-Control"] = f"public, max-age=0, s-maxage={ttl}, must-revalidate"
    except (AnswersUnavailable, OSError, sqlite3.Error, ValueError, KeyError):
        app.logger.exception("Could not serve daily Krillion answers")
        response = make_response(render_template("index.html", questions=[], unavailable=True), 503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = str(RETRY_SECONDS)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    return response


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
