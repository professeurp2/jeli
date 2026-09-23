"""Signing in to the dashboard.

One account per team member (DASHBOARD_USERS, salted scrypt hashes; a member may then choose their
own password, kept in jeli.dashboard_passwords). Signing in sets a signed session cookie; every
change also carries a token tied to that session, so another site cannot act for a signed-in member.
Password guessing is slowed down: five failures in 15 minutes, and that name or address must wait.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

SESSION_COOKIE = "jeli_session"
VISITOR_COOKIE = "jeli_visitor"
VISITOR_SECONDS = 24 * 3600
SESSION_SECONDS = 7 * 24 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
FAILURES_ALLOWED = 5
FAILURE_WINDOW = 15 * 60
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str, salt: bytes | None = None) -> str:
    """"salt:hash" in hex, the form an account keeps."""
    salt = salt or secrets.token_bytes(16)
    return f"{salt.hex()}:{hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt = bytes.fromhex(stored.split(":", 1)[0])
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


# Checked against when the name is unknown, so a wrong name takes as long as a wrong password.
_NOBODY = f"{'00' * 16}:{'00' * 32}"


class Auth:
    def __init__(self, accounts: dict[str, str], secret: str = "", store=None, clock=time.time):
        self.accounts = accounts  # name → "salt:hash", from the environment
        self.store = store
        self.clock = clock
        if not secret and accounts:
            log.warning("DASHBOARD_SECRET is not set: members must sign in again after each restart")
        self._secret = secret.encode() if secret else secrets.token_bytes(32)
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return bool(self.accounts)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    # --- Passwords -----------------------------------------------------------------------------

    async def _stored_hash(self, name: str) -> str | None:
        if name not in self.accounts:
            return None
        if self.store:
            chosen = (await self.store.password_hashes()).get(name)
            if chosen:
                return chosen
        return self.accounts[name]

    async def verify(self, name: str, password: str) -> str | None:
        """The account name if the password is right, else None."""
        name = name.strip().lower()
        stored = await self._stored_hash(name)
        valid = await asyncio.to_thread(check_password, password, stored or _NOBODY)
        return name if valid and stored else None

    async def change_password(self, name: str, current: str, new: str) -> str | None:
        """None when changed, else what is wrong, for the member."""
        if len(new) < MIN_PASSWORD_LENGTH:
            return f"The new password needs at least {MIN_PASSWORD_LENGTH} characters."
        if not await self.verify(name, current):
            return "Your current password is not right."
        if not self.store:
            return "Passwords can only be changed when the database is connected."
        await self.store.set_password_hash(name, await asyncio.to_thread(hash_password, new))
        await self.store.add_audit(name, "Changed their password")
        return None

    # --- Password guessing ---------------------------------------------------------------------

    def throttled(self, *keys: str) -> bool:
        now = self.clock()
        for key in keys:
            failures = self._failures[key]
            while failures and now - failures[0] > FAILURE_WINDOW:
                failures.popleft()
            if len(failures) >= FAILURES_ALLOWED:
                return True
        return False

    def failed(self, *keys: str) -> None:
        for key in keys:
            self._failures[key].append(self.clock())

    # --- Sessions ------------------------------------------------------------------------------

    def new_session(self, name: str) -> str:
        payload = f"{name}|{int(self.clock()) + SESSION_SECONDS}|{secrets.token_hex(8)}"
        return f"{payload}|{self._sign(payload)}"

    def member(self, session: str | None) -> str | None:
        """Who a session cookie belongs to, if it is genuine, current, and the account still exists."""
        if not session or session.count("|") != 3:
            return None
        payload, signature = session.rsplit("|", 1)
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        name, expires, _ = payload.split("|")
        if not expires.isdigit() or int(expires) < self.clock() or name not in self.accounts:
            return None
        return name

    def csrf(self, session: str) -> str:
        return self._sign(f"csrf|{session}")[:32]

    # --- Visitors -------------------------------------------------------------------------------
    # A member of the groups looking at Jeli's public page. They prove nothing but their number,
    # which Jeli already knows from the groups: the page is read-only and the chat is capped, so
    # the worst a wrong number buys is a look at what the community already sees every day.

    def new_visitor(self, number: str, name: str) -> str:
        payload = f"{number}|{' '.join(name.split()).replace('|', ' ')[:40]}|{int(self.clock()) + VISITOR_SECONDS}"
        return f"{payload}|{self._sign(payload)}"

    def visitor(self, cookie: str | None) -> tuple[str, str] | None:
        """(number, name) when the cookie is genuine and current, else None."""
        if not cookie or cookie.count("|") != 3:
            return None
        payload, signature = cookie.rsplit("|", 1)
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        number, name, expires = payload.split("|")
        if not expires.isdigit() or int(expires) < self.clock():
            return None
        return number, name


def client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")


def auth_of(request: Request) -> Auth:
    auth: Auth | None = getattr(request.app.state, "auth", None)
    if auth is None or not auth.enabled:
        raise HTTPException(status_code=404)
    return auth


def signed_in(request: Request) -> str:
    """Dependency: the signed-in member, or a redirect to the sign-in page."""
    auth = auth_of(request)
    name = auth.member(request.cookies.get(SESSION_COOKIE))
    if not name:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={quote(target)}"})
    return name


async def allowed_change(request: Request) -> str:
    """Dependency for every change: signed in, from this site, with the session's token."""
    name = signed_in(request)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403)
    session = request.cookies.get(SESSION_COOKIE, "")
    token = request.headers.get("x-csrf-token")
    if token is None:
        token = (await request.form()).get("csrf", "")
    if not hmac.compare_digest(str(token), auth_of(request).csrf(session)):
        raise HTTPException(status_code=403)
    return name


def safe_next(target: str | None) -> str:
    """Only a path on this site: never send a member elsewhere after signing in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/dashboard"
