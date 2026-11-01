"""Tests for the async Skylight client, run against a fake server."""

from datetime import date
import time
import unittest
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.skylink.api import (
    API_VERSION,
    SkylightAuthError,
    SkylightClient,
    SkylightConnectionError,
    Tokens,
)

PASSWORD = "correct-horse"
LOGIN_FORM = '<form><input name="authenticity_token" value="csrf123"></form>'
CHORES = {
    "data": [
        {
            "id": "1-2026-10-31",
            "attributes": {"summary": "Teeth", "routine": True, "start": "2026-10-31", "start_time": "06:00", "status": "pending"},
            "relationships": {"category": {"data": {"id": "77"}}},
        }
    ]
}


class FakeSkylight:
    def __init__(self):
        self.state = None
        self.valid_access = set()
        self.valid_refresh = set()
        self.counter = 0
        self.logins = 0
        self.refreshes = 0
        self.login_status = None
        self.refresh_status = None
        self.chores_status = None
        self.reject_all_tokens = False
        self.chores_queries = []

    def app(self):
        app = web.Application()
        app.router.add_get("/oauth/authorize", self.authorize)
        app.router.add_get("/auth/session/new", self.form)
        app.router.add_post("/auth/session", self.session)
        app.router.add_get("/welcome-hop", self.hop)
        app.router.add_post("/oauth/token", self.token)
        app.router.add_get("/api/frames", self.frames)
        app.router.add_get("/api/frames/9/chores", self.chores)
        return app

    def issue(self):
        self.counter += 1
        access, refresh = f"access-{self.counter}", f"refresh-{self.counter}"
        self.valid_access = {access}
        self.valid_refresh = {refresh}
        return web.json_response(
            {"access_token": access, "refresh_token": refresh, "expires_in": 86400, "created_at": int(time.time())}
        )

    async def authorize(self, request):
        self.state = request.query["state"]
        raise web.HTTPFound("/auth/session/new")

    async def form(self, request):
        return web.Response(text=LOGIN_FORM, content_type="text/html")

    async def session(self, request):
        data = await request.post()
        if self.login_status:
            return web.Response(status=self.login_status)
        if data["password"] != PASSWORD or data["authenticity_token"] != "csrf123":
            return web.Response(text=LOGIN_FORM, content_type="text/html")
        raise web.HTTPFound("/welcome-hop")

    async def hop(self, request):
        return web.Response(status=302, headers={"Location": f"skylight-family://welcome?code=abc&state={self.state}"})

    async def token(self, request):
        form = parse_qs((await request.read()).decode())
        if form["grant_type"][0] == "refresh_token":
            self.refreshes += 1
            if self.refresh_status:
                return web.Response(status=self.refresh_status)
            if form["refresh_token"][0] not in self.valid_refresh:
                return web.Response(status=400)
        else:
            self.logins += 1
            if not form.get("code_verifier"):
                return web.Response(status=400)
        return self.issue()

    def authorized(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return (
            not self.reject_all_tokens
            and token in self.valid_access
            and request.headers.get("skylight-api-version") == API_VERSION
        )

    async def frames(self, request):
        if not self.authorized(request):
            return web.Response(status=401)
        return web.json_response({"data": [{"id": "9"}]})

    async def chores(self, request):
        if not self.authorized(request):
            return web.Response(status=401)
        if self.chores_status:
            return web.Response(status=self.chores_status)
        self.chores_queries.append(dict(request.query))
        return web.json_response(CHORES)


class ClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSkylight()
        self.server = TestServer(self.fake.app())
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.saved = []

    def client(self, password=PASSWORD, tokens=None):
        return SkylightClient(
            self.session,
            "parent@example.com",
            password,
            tokens=tokens,
            on_tokens=self.saved.append,
            base_url=str(self.server.make_url("/")),
        )

    async def test_first_request_logs_in_and_saves_tokens(self):
        client = self.client()
        tasks = await client.async_list_tasks("9", date(2026, 10, 31))
        self.assertEqual(self.fake.logins, 1)
        self.assertEqual([t.summary for t in tasks], ["Teeth"])
        self.assertEqual(self.saved[-1].refresh_token, "refresh-1")

    async def test_tasks_are_requested_for_exactly_one_day(self):
        await self.client().async_list_tasks("9", date(2026, 10, 31))
        self.assertEqual(
            self.fake.chores_queries, [{"after": "2026-10-31", "before": "2026-10-31", "include_late": "true"}]
        )

    async def test_wrong_password_raises_auth_error(self):
        with self.assertRaises(SkylightAuthError):
            await self.client(password="wrong").async_login()

    async def test_401_refreshes_retries_and_saves_the_rotated_token(self):
        client = self.client()
        await client.async_login()
        self.fake.valid_access = set()
        self.assertEqual(len(await client.async_list_frames()), 1)
        self.assertEqual((self.fake.logins, self.fake.refreshes), (1, 1))
        self.assertEqual(self.saved[-1].refresh_token, "refresh-2")

    async def test_expired_token_is_refreshed_before_the_request(self):
        client = self.client()
        await client.async_login()
        client.tokens.expires_at = time.time() - 1
        await client.async_list_frames()
        self.assertEqual(self.fake.refreshes, 1)

    async def test_refused_refresh_falls_back_to_login(self):
        client = self.client()
        await client.async_login()
        self.fake.valid_access = set()
        self.fake.valid_refresh = set()
        await client.async_list_frames()
        self.assertEqual((self.fake.logins, self.fake.refreshes), (2, 1))

    async def test_stored_tokens_are_used_without_logging_in(self):
        self.fake.valid_access = {"stored"}
        client = self.client(tokens=Tokens("stored", "stored-refresh", time.time() + 3600))
        await client.async_list_frames()
        self.assertEqual(self.fake.logins, 0)

    async def test_auth_error_when_password_changed_and_tokens_are_dead(self):
        client = self.client(password="old-password", tokens=Tokens("dead", "dead", time.time() + 3600))
        with self.assertRaises(SkylightAuthError):
            await client.async_list_frames()

    async def test_auth_error_when_new_token_is_also_rejected(self):
        client = self.client()
        await client.async_login()
        self.fake.reject_all_tokens = True
        with self.assertRaises(SkylightAuthError):
            await client.async_list_frames()

    async def test_server_error_is_retryable_not_an_auth_failure(self):
        client = self.client()
        await client.async_login()
        self.fake.chores_status = 503
        with self.assertRaises(SkylightConnectionError):
            await client.async_list_tasks("9", date(2026, 10, 31))

    async def test_refresh_server_error_does_not_attempt_login(self):
        client = self.client()
        await client.async_login()
        self.fake.valid_access = set()
        self.fake.refresh_status = 503
        with self.assertRaises(SkylightConnectionError):
            await client.async_list_frames()
        self.assertEqual(self.fake.logins, 1)

    async def test_login_server_error_is_retryable(self):
        self.fake.login_status = 503
        with self.assertRaises(SkylightConnectionError):
            await self.client().async_login()

    async def test_unreachable_server_is_retryable(self):
        client = SkylightClient(
            self.session, "e", "p", tokens=Tokens("t", None, None), base_url="http://127.0.0.1:9"
        )
        with self.assertRaises(SkylightConnectionError):
            await client.async_list_frames()


if __name__ == "__main__":
    unittest.main()
