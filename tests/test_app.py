import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

import app as reader


NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc).timestamp()
TODAY = {
    "date": "2026-11-24", "resetsAt": "2026-11-25T05:00:00Z",
    "prompts": [{"id": "p1", "text": "Name a fictional sport"}],
}
REVEAL = {
    "date": "2026-11-24",
    "prompts": [{"id": "p1", "text": "Name a fictional sport", "answers": [
        {"answer": "Ordinary pick", "tier": "deepcut", "score": 85},
        {"answer": "Imaginary ball", "tier": "krillion", "score": 100, "quip": "A fictional explanation."},
    ]}],
}


class SchemaTests(unittest.TestCase):
    def test_retains_only_krillion_and_its_published_explanation(self):
        result = reader.normalize(TODAY, REVEAL, NOW)
        self.assertEqual(result["questions"][0]["answers"], [
            {"answer": "Imaginary ball", "explanation": "A fictional explanation."},
        ])

    def test_retains_multiple_top_answers(self):
        sheet = copy.deepcopy(REVEAL)
        sheet["prompts"][0]["answers"].append({"answer": "Other pick", "tier": "krillion", "score": 100})
        answers = reader.normalize(TODAY, sheet, NOW)["questions"][0]["answers"]
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[1]["explanation"], "No explanation published.")

    def test_does_not_substitute_a_lower_tier(self):
        sheet = copy.deepcopy(REVEAL)
        sheet["prompts"][0]["answers"] = sheet["prompts"][0]["answers"][:1]
        self.assertEqual(reader.normalize(TODAY, sheet, NOW)["questions"][0]["answers"], [])

    def test_rejects_wrong_dates_prompts_and_resets(self):
        cases = []
        wrong_date = copy.deepcopy(REVEAL)
        wrong_date["date"] = "2026-11-23"
        cases.append((TODAY, wrong_date))
        wrong_prompt = copy.deepcopy(REVEAL)
        wrong_prompt["prompts"][0]["text"] = "A different prompt"
        cases.append((TODAY, wrong_prompt))
        missing = copy.deepcopy(REVEAL)
        missing["prompts"] = []
        cases.append((TODAY, missing))
        for reset in ("2026-11-24T05:00:00Z", "2026-11-25T05:00:00", "2026-12-25T05:00:00Z"):
            today = copy.deepcopy(TODAY)
            today["resetsAt"] = reset
            cases.append((today, REVEAL))
        for today, reveal in cases:
            with self.subTest(today=today, reveal=reveal):
                with self.assertRaises(reader.AnswersUnavailable):
                    reader.normalize(today, reveal, NOW)

    def test_joins_prompts_by_id_not_array_order(self):
        today, reveal = copy.deepcopy(TODAY), copy.deepcopy(REVEAL)
        today["prompts"].append({"id": "p2", "text": "Another prompt"})
        reveal["prompts"].insert(0, {"id": "p2", "text": "Another prompt", "answers": []})
        result = reader.normalize(today, reveal, NOW)
        self.assertEqual(result["questions"][0]["answers"][0]["answer"], "Imaginary ball")
        self.assertEqual(result["questions"][1]["answers"], [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "answers.sqlite3"
        self.now = NOW
        self.calls = 0
        self.daily = reader.normalize(TODAY, REVEAL, NOW)

    def load(self):
        self.calls += 1
        return self.daily

    def get(self, loader=None):
        return reader.cached_daily(self.path, loader or self.load, lambda: self.now)

    def test_repeated_requests_share_disk_cache(self):
        self.assertEqual(self.get(), self.daily)
        self.assertEqual(self.get(), self.daily)
        self.assertEqual(self.calls, 1)

    def test_refreshes_at_reset(self):
        self.get()
        self.now = self.daily["expires_at"]
        self.daily = {**self.daily, "date": "2026-11-25", "expires_at": self.now + 86400}
        self.assertEqual(self.get()["date"], "2026-11-25")
        self.assertEqual(self.calls, 2)

    def test_error_cooldown_and_recovery(self):
        def offline():
            self.calls += 1
            raise URLError("offline")

        for _ in range(3):
            with self.assertRaises(reader.AnswersUnavailable):
                self.get(offline)
        self.assertEqual(self.calls, 1)
        self.now += reader.RETRY_SECONDS
        self.assertEqual(self.get(), self.daily)
        self.assertEqual(self.calls, 2)

    def test_never_serves_yesterday_after_reset(self):
        self.get()
        self.now = self.daily["expires_at"]
        with self.assertRaises(reader.AnswersUnavailable):
            self.get(lambda: self.daily)
        with self.assertRaises(reader.AnswersUnavailable):
            self.get()

    def test_concurrent_refreshes_only_fetch_once(self):
        barrier = threading.Barrier(6)

        def request(_):
            barrier.wait(timeout=5)
            return self.get()

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(request, range(6)))
        self.assertEqual(results, [self.daily] * 6)
        self.assertEqual(self.calls, 1)


class PageTests(unittest.TestCase):
    def setUp(self):
        self.client = reader.app.test_client()

    def test_renders_and_escapes_upstream_text_with_reset_limited_cache(self):
        daily = reader.normalize(TODAY, REVEAL, NOW)
        daily["questions"][0]["answers"][0]["answer"] = '<script>alert("x")</script>'
        with patch.object(reader, "cached_daily", return_value=daily), patch.object(reader.time, "time", return_value=NOW):
            response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("I don't want to play Krillion", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("Fictional".lower(), html.lower())
        self.assertIn("s-maxage=61195", response.headers["Cache-Control"])
        self.assertIn("If you use this to cheat, you should find something better to do with your life", html)

    def test_unavailable_is_503_and_not_cached(self):
        with patch.object(reader, "cached_daily", side_effect=reader.AnswersUnavailable("offline")):
            with self.assertLogs(reader.app.logger, level="ERROR"):
                response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Retry-After"], "60")


if __name__ == "__main__":
    unittest.main()
