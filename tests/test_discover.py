"""Offline tests for tools/discover.py, run against a fake Skylight server."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import discover  # noqa: E402

EMAIL = "parent@example.com"
PASSWORD = "correct-horse"
LOGIN_FORM = b'<form><input name="authenticity_token" value="csrf123"></form>'
CHORES = {
    "data": [
        {
            "type": "chore",
            "id": "86753099-2026-10-30",
            "attributes": {
                "summary": "Brush teeth",
                "routine": True,
                "start_time": "07:00",
                "status": "complete",
                "recurring": True,
                "recurrence_set": "RRULE:FREQ=DAILY",
                "reward_points": 2,
            },
            "relationships": {"category": {"data": {"type": "category", "id": "77"}}},
        },
        {
            "type": "chore",
            "id": "556",
            "attributes": {
                "summary": "Tell Mia hello",
                "routine": False,
                "start_time": None,
                "status": "pending",
                "recurring": False,
            },
            "relationships": {"category": {"data": {"type": "category", "id": "78"}}},
        },
    ]
}
CATEGORIES = {
    "data": [
        {"type": "category", "id": "77", "attributes": {"label": "Mia", "linked_to_profile": True}},
        {"type": "category", "id": "78", "attributes": {"label": "leo", "linked_to_profile": True}},
    ]
}


def make_handler(pkce_enabled=True):
    class Handler(BaseHTTPRequestHandler):
        logged_in = False
        state = None
        refresh_forms = []

        def log_message(self, *args):
            pass

        def _send(self, status, body=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload):
            self._send(200, json.dumps(payload).encode(), {"Content-Type": "application/json"})

        def _authorized(self):
            if self.headers.get("Authorization") != "Bearer access-1":
                self._send(401)
                return False
            return self.headers.get("skylight-api-version") == discover.API_VERSION

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/oauth/authorize":
                if "code_challenge" in query:
                    if not pkce_enabled:
                        self._send(400)
                        return
                    type(self).state = query["state"][0]
                    self._send(302, headers={"Location": "/auth/session/new"})
                elif type(self).logged_in:
                    self._send(302, headers={"Location": "https://ourskylight.com/welcome?code=web-code"})
                else:
                    self._send(302, headers={"Location": "/auth/session/new"})
            elif parsed.path == "/auth/session/new":
                self._send(200, LOGIN_FORM)
            elif parsed.path == "/api/frames":
                if self._authorized():
                    self._json({"data": [{"type": "frame", "id": "9", "attributes": {"name": "Smith Family"}}]})
            elif parsed.path == "/api/frames/9/categories":
                if self._authorized():
                    self._json(CATEGORIES)
            elif parsed.path == "/api/frames/9/chores":
                if self._authorized():
                    self._json(CHORES)
            elif parsed.path == "/api/frames/9/routines":
                if self._authorized():
                    self._json({"data": [{"id": "1", "attributes": {"period": "morning", "schedule_note": "Bedtime for Mia"}}]})
            else:
                self._send(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            if self.path == "/auth/session":
                if form.get("password") != PASSWORD or form.get("authenticity_token") != "csrf123":
                    self._send(200, LOGIN_FORM)
                    return
                type(self).logged_in = True
                location = f"skylight-family://welcome?code=app-code&state={type(self).state}" if pkce_enabled else "/"
                self._send(302, headers={"Location": location})
            elif self.path == "/oauth/token":
                refresh_token = "refresh-1"
                if form["grant_type"] == "refresh_token":
                    type(self).refresh_forms.append(form)
                    refresh_token = "refresh-2"
                self._json({"access_token": "access-1", "refresh_token": refresh_token, "expires_in": 7200})
            else:
                self._send(404)

    return Handler


class FakeServerTest(unittest.TestCase):
    pkce_enabled = True

    def setUp(self):
        self.handler = make_handler(self.pkce_enabled)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_main(self, *extra, password=PASSWORD):
        env = {"SKYLIGHT_EMAIL": EMAIL, "SKYLIGHT_PASSWORD": password}
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--base-url", self.base_url,
            "--output", str(self.tmp / "out"),
            "--salt-file", str(self.tmp / "salt"),
            "--timezone", "America/Chicago",
            *extra,
        ]
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = discover.main(argv)
        return code, out.getvalue(), err.getvalue()

    def saved_text(self):
        return "\n".join(p.read_text() for p in (self.tmp / "out").rglob("*.json"))


class PkceFlowTest(FakeServerTest):
    def test_end_to_end_capture_and_report(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("pkce flow", out)
        self.assertIn("Today's tasks: 2", out)
        self.assertIn("start_time: ['\"07:00\"', 'null']", out)
        self.assertIn('period: [\'"morning"\']', out)
        self.assertIn("works, refresh token rotated", out)
        self.assertNotIn("skylight_api_client_device_fingerprint", self.handler.refresh_forms[0])

    def test_nothing_personal_is_saved_or_printed_by_default(self):
        _, out, err = self.run_main()
        saved = self.saved_text()
        secrets_ = (EMAIL, PASSWORD, "access-1", "refresh-1", "refresh-2", "Mia", "leo", "Brush teeth",
                    "Smith Family", "86753099", "Bedtime for Mia")
        for secret in secrets_:
            self.assertNotIn(secret, saved, secret)
            self.assertNotIn(secret, out, secret)
            self.assertNotIn(secret, err, secret)

    def test_structure_needed_for_the_gate_survives_redaction(self):
        self.run_main()
        path = next((self.tmp / "out").rglob("chores_today.json"))
        attrs = json.loads(path.read_text())["body"]["data"][0]
        self.assertTrue(attrs["id"].endswith("-2026-10-30"))
        self.assertEqual(attrs["attributes"]["start_time"], "07:00")
        self.assertEqual(attrs["attributes"]["status"], "complete")
        self.assertEqual(attrs["attributes"]["routine"], True)
        self.assertEqual(attrs["attributes"]["recurrence_set"], "RRULE:FREQ=DAILY")

    def test_show_names_prints_real_names(self):
        _, out, _ = self.run_main("--show-names")
        self.assertIn("Mia", out)
        self.assertIn("Brush teeth", out)
        self.assertNotIn("Mia", self.saved_text())

    def test_redaction_is_stable_between_runs(self):
        self.run_main()
        first = sorted((self.tmp / "out").rglob("chores_today.json"))[0].read_text()
        salt = (self.tmp / "salt").read_bytes()
        redactor = discover.Redactor(salt, {"Mia", "leo"})
        self.assertEqual(json.loads(first)["body"]["data"][0]["attributes"]["summary"], redactor.token("Brush teeth"))

    def test_wrong_password_stops_after_one_attempt(self):
        with mock.patch.object(discover, "login_fingerprint") as fallback:
            code, out, err = self.run_main(password="wrong")
        self.assertEqual(code, 1)
        self.assertIn("rejected the login", err)
        fallback.assert_not_called()
        self.assertNotIn("wrong", out + err)


class FingerprintFallbackTest(FakeServerTest):
    pkce_enabled = False

    def test_falls_back_when_pkce_is_refused(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("fingerprint flow", out)
        self.assertIn("skylight_api_client_device_fingerprint", self.handler.refresh_forms[0])


class LateReportTest(unittest.TestCase):
    def test_lists_late_records_redacted_and_flags_repeats(self):
        def chore(start, status="pending", completed_on=None):
            return {
                "attributes": {
                    "start": start, "summary": "Cut Cardboard", "status": status, "series": "s1",
                    "completed_on": completed_on, "routine": False, "recurring": True, "start_time": None,
                },
                "relationships": {"category": {"data": {"id": "77"}}},
            }

        chores = [chore("2026-10-30", "complete", "2026-11-01"), chore("2026-10-31"), chore("2026-11-01")]
        redactor = discover.Redactor(b"salt")
        text = "\n".join(discover.build_late_report(chores, {"77": "Mia"}, "2026-11-01", redactor, False))
        self.assertIn("Late records with include_late=true: 2", text)
        self.assertIn("Recurring chores with more than one late record: 1", text)
        self.assertNotIn("Cut Cardboard", text)
        self.assertNotIn("Mia", text)


class SafetyTest(unittest.TestCase):
    def test_refuses_plain_http_to_a_remote_host(self):
        with self.assertRaises(SystemExit):
            discover.validate_base_url("http://example.com")

    def test_several_calendars_requires_a_choice(self):
        with self.assertRaises(SystemExit):
            discover.choose_frame([{"id": 1}, {"id": 2}], None)
        self.assertEqual(discover.choose_frame([{"id": 1}, {"id": 2}], "2"), "2")


if __name__ == "__main__":
    unittest.main()
