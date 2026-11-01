#!/usr/bin/env python3
"""Phase 0 discovery: capture what the Skylight API really returns for your account.

Standard library only. It sends reads only after login, and it never stores your
password or tokens. Captures are redacted before they are written, and the printed
report is redacted too unless you pass --show-names.

    python3 tools/discover.py --timezone America/Chicago

Credentials come from SKYLIGHT_EMAIL and SKYLIGHT_PASSWORD, or a prompt.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASE_URL = "https://app.ourskylight.com"
API_VERSION = "2026-03-01"
CLIENT_ID = "skylight-mobile"
SCOPE = "everything"
APP_REDIRECT_URI = "skylight-family://welcome"
WEB_REDIRECT_URI = "https://ourskylight.com/welcome"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
REDIRECT_CODES = (301, 302, 303, 307, 308)
CSRF_RE = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')
CSRF_META_RE = re.compile(r'name="csrf-token"[^>]*content="([^"]+)"')

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "discovery"
SALT_FILE = REPO_ROOT / ".discover-salt"

SENSITIVE_KEYS = {
    "address", "avatar", "avatar_url", "description", "display_name", "email", "first_name",
    "full_name", "image_url", "label", "last_name", "name", "nickname", "note", "notes",
    "phone", "photo_url", "profile_pic_url", "summary", "time_zone", "timezone", "title",
    "token", "url", "username",
}
SAFE_STRING_PATTERNS = [
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?( ?[ap]m)?$", re.I),
    re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$"),
    re.compile(r"^(RRULE|EXDATE|DTSTART)[:;]"),
    re.compile(r"^[a-z][a-z0-9_-]{0,23}$"),
]
SAFE_WORDS = {"morning", "afternoon", "evening", "night", "anytime", "all day"}
TIME_KEY_RE = re.compile(r"time|period|part|routine|daypart|schedule|slot|morning|afternoon|evening")
DATE_SUFFIX_RE = re.compile(r"^(.*?)(-\d{4}-\d{2}-\d{2})$")


class AuthError(Exception):
    """Login or token exchange failed."""


class BadCredentials(AuthError):
    """Skylight rejected the email or password, so retrying another flow won't help."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Response:
    def __init__(self, status: int, headers: Any, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body)


class Http:
    """Cookie-keeping HTTP client that never follows redirects on its own."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as err:
            return Response(err.code, err.headers, err.read())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _browser_headers(**extra: str) -> dict[str, str]:
    return {"User-Agent": BROWSER_UA, "Accept": BROWSER_ACCEPT, **extra}


def _extract_csrf(html: str) -> str | None:
    match = CSRF_RE.search(html) or CSRF_META_RE.search(html)
    return match.group(1) if match else None


def _follow(http_client: Http, resp: Response, stop_scheme: str | None, hops: int = 10) -> tuple[Response, str | None]:
    """Follow redirects, stopping at the app-scheme location if one appears."""
    location = resp.headers.get("location")
    while resp.status in REDIRECT_CODES and location and hops > 0:
        if stop_scheme and location.startswith(stop_scheme):
            return resp, location
        resp = http_client.request("GET", location, headers=_browser_headers())
        location = resp.headers.get("location")
        hops -= 1
    return resp, location


def _post_session(http_client: Http, csrf: str, email: str, password: str) -> Response:
    resp = http_client.request(
        "POST",
        "/auth/session",
        form={"authenticity_token": csrf, "email": email, "password": password},
        headers=_browser_headers(
            Origin=http_client.base_url, Referer=http_client.base_url + "/auth/session/new"
        ),
    )
    # Rails re-renders the form with a 200 when the credentials are wrong.
    if resp.status == 200:
        raise BadCredentials(
            "Skylight rejected the login. Check the email and password. An account that "
            "signs in through Google or Apple has no password this flow can use."
        )
    return resp


def _exchange(http_client: Http, form: dict[str, str]) -> dict[str, Any]:
    resp = http_client.request(
        "POST", "/oauth/token", form=form, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}
    )
    if resp.status != 200:
        raise AuthError(f"/oauth/token returned HTTP {resp.status}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AuthError("/oauth/token did not return JSON") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AuthError("/oauth/token response had no access_token")
    return payload


def login_pkce(http_client: Http, email: str, password: str) -> dict[str, Any]:
    """Flow used by pyskylight: start at /oauth/authorize with PKCE."""
    verifier = _b64url(secrets.token_bytes(32))
    state = _b64url(secrets.token_bytes(18))
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": APP_REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    resp = http_client.request("GET", "/oauth/authorize", params=params, headers=_browser_headers())
    resp, _ = _follow(http_client, resp, "skylight-family:")
    csrf = _extract_csrf(resp.text)
    if not csrf:
        raise AuthError("no CSRF token on the login page")
    resp = _post_session(http_client, csrf, email, password)
    _, location = _follow(http_client, resp, "skylight-family:")
    if not (location and location.startswith("skylight-family:")):
        raise AuthError("login did not redirect back to the app")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    if query.get("state", [""])[0] != state:
        raise AuthError("OAuth state mismatch")
    code = query.get("code", [""])[0]
    if not code:
        raise AuthError("no authorization code in the redirect")
    return _exchange(
        http_client,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": APP_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )


def login_fingerprint(http_client: Http, email: str, password: str, fingerprint: str) -> dict[str, Any]:
    """Flow used by skylightctl: log in first, then authorize with a device fingerprint."""
    resp = http_client.request("GET", "/auth/session/new", headers=_browser_headers())
    csrf = _extract_csrf(resp.text)
    if not csrf:
        raise AuthError("no CSRF token on the login page")
    _post_session(http_client, csrf, email, password)
    resp = http_client.request(
        "GET",
        "/oauth/authorize",
        params={
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": WEB_REDIRECT_URI,
            "scope": SCOPE,
            "skylight_api_client_device_fingerprint": fingerprint,
        },
        headers=_browser_headers(),
    )
    location = resp.headers.get("location") or ""
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
    if not code:
        raise AuthError("no authorization code in the redirect")
    return _exchange(
        http_client,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": WEB_REDIRECT_URI,
            "scope": SCOPE,
            "skylight_api_client_device_fingerprint": fingerprint,
        },
    )


def login(base_url: str, email: str, password: str) -> tuple[dict[str, Any], str, str | None]:
    """Try each known flow. Returns the token payload, the flow name and any fingerprint."""
    failures = []
    try:
        return login_pkce(Http(base_url), email, password), "pkce", None
    except BadCredentials:
        raise
    except AuthError as err:
        failures.append(f"pkce: {err}")
    fingerprint = str(uuid.uuid4())
    try:
        return login_fingerprint(Http(base_url), email, password, fingerprint), "fingerprint", fingerprint
    except BadCredentials:
        raise
    except AuthError as err:
        failures.append(f"fingerprint: {err}")
    raise AuthError("; ".join(failures))


def test_refresh(base_url: str, token: dict[str, Any], fingerprint: str | None) -> str:
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return "no refresh token was issued"
    form = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token}
    if fingerprint:
        form["skylight_api_client_device_fingerprint"] = fingerprint
    try:
        new = _exchange(Http(base_url), form)
    except AuthError as err:
        return f"failed ({err})"
    rotated = new.get("refresh_token") not in (None, refresh_token)
    return f"works, refresh token {'rotated' if rotated else 'unchanged'}"


class Redactor:
    """Scrubs personal values while keeping every key and any enum-like or date value.

    Unknown keys must survive, because the point of the capture is to find fields
    nobody has documented. Values are replaced with salted hashes that stay stable
    across runs, so snapshots from different times can still be compared.
    """

    def __init__(self, salt: bytes, known_values: set[str] | None = None) -> None:
        self.salt = salt
        self.known = {value.casefold() for value in known_values or set()}

    def token(self, value: Any, prefix: str = "str") -> str:
        digest = hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _id(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        match = DATE_SUFFIX_RE.match(str(value))
        if match:
            return self.token(match.group(1), "id") + match.group(2)
        return self.token(value, "id")

    def _string(self, value: str) -> str:
        if value.casefold() in self.known:
            return self.token(value)
        if value.casefold() in SAFE_WORDS or any(p.match(value) for p in SAFE_STRING_PATTERNS):
            return value
        return self.token(value)

    def scrub(self, value: Any, key: str = "") -> Any:
        lowered = key.lower()
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(item, key) for item in value]
        if lowered == "id" or lowered.endswith("_id") or lowered.endswith("_ids"):
            return self._id(value)
        if value is None or isinstance(value, bool):
            return value
        if lowered in SENSITIVE_KEYS:
            return self.token(value)
        if isinstance(value, str):
            return self._string(value)
        return value


def load_salt(path: Path = SALT_FILE) -> bytes:
    if path.exists():
        return path.read_bytes()
    salt = secrets.token_bytes(32)
    path.touch(mode=0o600)
    path.write_bytes(salt)
    return salt


def collect_known_values(payload: Any) -> set[str]:
    """Pull names out of a response so they are also scrubbed wherever else they appear."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in SENSITIVE_KEYS and isinstance(value, str) and value:
                found.add(value)
            else:
                found |= collect_known_values(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= collect_known_values(item)
    return found


def resources(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data")
    else:
        data = payload
    if isinstance(data, dict):
        data = [data]
    return [item for item in data or [] if isinstance(item, dict)] if isinstance(data, list) else []


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes")
    return attrs if isinstance(attrs, dict) else item


def _category_id(item: dict[str, Any]) -> str | None:
    rel = (item.get("relationships") or {}).get("category")
    data = rel.get("data") if isinstance(rel, dict) else None
    return str(data["id"]) if isinstance(data, dict) and data.get("id") is not None else None


def time_of_day_candidates(items: list[dict[str, Any]], redactor: Redactor) -> dict[str, list[str]]:
    """Every attribute whose name hints at timing, with the distinct redacted values seen."""
    seen: dict[str, set[str]] = {}
    for item in items:
        for key, value in _attributes(item).items():
            if TIME_KEY_RE.search(key.lower()):
                seen.setdefault(key, set()).add(json.dumps(redactor.scrub(value, key), sort_keys=True))
    return {key: sorted(values) for key, values in sorted(seen.items())}


def build_report(
    chores: list[dict[str, Any]],
    routines: list[dict[str, Any]],
    labels: dict[str, str],
    redactor: Redactor,
    show_names: bool,
) -> list[str]:
    def shown(value: Any) -> str:
        return str(value) if show_names else redactor.token(value)

    lines = [f"Today's tasks: {len(chores)}", ""]
    lines.append(f"{'routine':<8}{'start_time':<12}{'status':<11}{'recurring':<11}{'child':<20}task")
    for item in sorted(chores, key=lambda c: str(_attributes(c).get("start_time") or "")):
        attrs = _attributes(item)
        category = _category_id(item)
        child = shown(labels.get(category or "", category or "none"))
        lines.append(
            f"{str(attrs.get('routine')):<8}{str(attrs.get('start_time')):<12}"
            f"{str(attrs.get('status')):<11}{str(attrs.get('recurring')):<11}"
            f"{child:<20}{shown(attrs.get('summary'))}"
        )

    lines += ["", "Status counts: " + json.dumps(Counter(str(_attributes(c).get("status")) for c in chores))]
    lines.append("Routine flag counts: " + json.dumps(Counter(str(_attributes(c).get("routine")) for c in chores)))
    keys = sorted({key for item in chores for key in _attributes(item)})
    lines.append("Chore attribute names: " + ", ".join(keys))

    lines += ["", "Timing fields on chores:"]
    lines += [f"  {k}: {v}" for k, v in time_of_day_candidates(chores, redactor).items()] or ["  none found"]

    lines += ["", f"Records from /routines: {len(routines)}", "Timing fields on /routines:"]
    lines += [f"  {k}: {v}" for k, v in time_of_day_candidates(routines, redactor).items()] or ["  none found"]

    by_summary: dict[str, set[str]] = {}
    for item in chores:
        by_summary.setdefault(str(_attributes(item).get("summary")), set()).add(_category_id(item) or "none")
    shared = sum(1 for cats in by_summary.values() if len(cats) > 1)
    lines += ["", f"Task names appearing under more than one child: {shared}"]
    return lines


def build_late_report(
    chores: list[dict[str, Any]],
    labels: dict[str, str],
    today: str,
    redactor: Redactor,
    show_names: bool,
) -> list[str]:
    """Describe what include_late=true adds: tasks dated before today."""

    def shown(value: Any) -> str:
        return str(value) if show_names else redactor.token(value)

    late = [c for c in chores if str(_attributes(c).get("start") or "9999") < today]
    undated = sum(1 for c in chores if not _attributes(c).get("start"))
    lines = [f"Late records with include_late=true: {len(late)} (undated records: {undated})", ""]
    lines.append(f"{'routine':<8}{'start':<12}{'time':<7}{'status':<11}{'done today':<11}{'recurring':<10}{'child':<20}task")
    for item in sorted(late, key=lambda c: str(_attributes(c).get("start"))):
        attrs = _attributes(item)
        category = _category_id(item)
        child = shown(labels.get(category or "", category or "none"))
        lines.append(
            f"{str(attrs.get('routine')):<8}{str(attrs.get('start')):<12}{str(attrs.get('start_time')):<7}"
            f"{str(attrs.get('status')):<11}{str(attrs.get('completed_on') == today):<11}"
            f"{str(attrs.get('recurring')):<10}{child:<20}{shown(attrs.get('summary'))}"
        )
    per_series = Counter(str(_attributes(c).get("series")) for c in late)
    repeated = sum(1 for count in per_series.values() if count > 1)
    lines += ["", f"Recurring chores with more than one late record: {repeated}"]
    return lines


def choose_frame(frames: list[dict[str, Any]], requested: str | None) -> str:
    ids = [str(frame.get("id")) for frame in frames]
    if requested:
        return requested
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise SystemExit("No calendars found on this account. Pass --frame-id if you know yours.")
    listing = ", ".join(ids)
    raise SystemExit(f"This account has several calendars. Re-run with --frame-id, one of: {listing}")


def validate_base_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("Refusing to send credentials over a connection that isn't HTTPS.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timezone", help="IANA name of your household's timezone. Default: this computer's.")
    parser.add_argument("--frame-id", help="Calendar id, needed only if the account has several.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write redacted captures.")
    parser.add_argument("--show-names", action="store_true", help="Print real names. Don't share that output.")
    parser.add_argument("--base-url", default=BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--salt-file", type=Path, default=SALT_FILE, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_base_url(args.base_url)
    tz = ZoneInfo(args.timezone) if args.timezone else None
    now = datetime.now(tz).astimezone(tz)

    email = os.environ.get("SKYLIGHT_EMAIL") or input("Skylight email: ")
    password = os.environ.get("SKYLIGHT_PASSWORD") or getpass.getpass("Skylight password: ")
    try:
        token, flow, fingerprint = login(args.base_url, email, password)
    except AuthError as err:
        print(f"Login failed: {err}", file=sys.stderr)
        return 1
    print(f"Login worked using the {flow} flow. Access token lifetime: {token.get('expires_in')} seconds.")

    api = Http(args.base_url)
    headers = {
        "Authorization": f"Bearer {token['access_token']}",
        "skylight-api-version": API_VERSION,
        "Accept": "application/json",
        "User-Agent": "SkyLink-discover",
    }

    def get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = api.request("GET", path, params=params, headers=headers)
        try:
            body = resp.json() if resp.body else None
        except ValueError:
            body = None
        return {"http_status": resp.status, "params": params, "body": body}

    frames = get("/api/frames")
    if frames["http_status"] != 200 and not args.frame_id:
        print(f"Listing calendars returned HTTP {frames['http_status']}.", file=sys.stderr)
        return 1
    frame_id = choose_frame(resources(frames["body"]), args.frame_id)

    today = now.date()
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    frame_path = f"/api/frames/{frame_id}"
    captures = {
        "categories": get(f"{frame_path}/categories"),
        "chores_today": get(
            f"{frame_path}/chores",
            {"after": today.isoformat(), "before": today.isoformat(), "include_late": "false"},
        ),
        "chores_window": get(
            f"{frame_path}/chores",
            {"after": yesterday.isoformat(), "before": tomorrow.isoformat(), "include_late": "false"},
        ),
        "chores_with_late": get(
            f"{frame_path}/chores",
            {"after": today.isoformat(), "before": today.isoformat(), "include_late": "true"},
        ),
        "routines": get(f"{frame_path}/routines"),
    }

    known = {email} | collect_known_values(captures["categories"]["body"]) | collect_known_values(frames["body"])
    redactor = Redactor(load_salt(args.salt_file), known)
    snapshot_dir = args.output / now.strftime("%Y%m%dT%H%M%S")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for name, capture in captures.items():
        (snapshot_dir / f"{name}.json").write_text(
            json.dumps(redactor.scrub(capture), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    (snapshot_dir / "meta.json").write_text(
        json.dumps(
            {
                "captured_local": now.isoformat(),
                "login_flow": flow,
                "access_token_expires_in": token.get("expires_in"),
                "http_status": {name: capture["http_status"] for name, capture in captures.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    labels = {}
    for category in resources(captures["categories"]["body"]):
        attrs = _attributes(category)
        labels[str(category.get("id"))] = str(attrs.get("label") or attrs.get("name") or category.get("id"))

    print(f"Local time: {now.isoformat()}")
    print("HTTP status: " + json.dumps({name: capture["http_status"] for name, capture in captures.items()}))
    print()
    report = build_report(
        resources(captures["chores_today"]["body"]),
        resources(captures["routines"]["body"]),
        labels,
        redactor,
        args.show_names,
    )
    print("\n".join(report))
    window = resources(captures["chores_window"]["body"])
    print(f"Tasks in the yesterday-to-tomorrow window: {len(window)}")
    print()
    print(
        "\n".join(
            build_late_report(
                resources(captures["chores_with_late"]["body"]), labels, today.isoformat(), redactor, args.show_names
            )
        )
    )
    print()
    print(f"Token refresh: {test_refresh(args.base_url, token, fingerprint)}")
    print(f"\nRedacted captures saved to {snapshot_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
