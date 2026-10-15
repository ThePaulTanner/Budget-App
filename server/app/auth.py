"""Login: password + authenticator code, short-lived access tokens, rotating refresh tokens.

Design (see docs/SECURITY.md):
- Passwords: Argon2id (64 MiB, 3 passes) over an HMAC of the password with a server
  pepper that is derived from the master key and never stored in the database.
- Second factor: TOTP with replay protection, or a single-use recovery code.
- Access token: HS256 JWT valid for 15 minutes, tied to one device session.
- Refresh token: random 256-bit value, replaced on every use, stored only as a hash.
  The previous token keeps working for 60 seconds so an app killed mid-refresh can
  retry. Using it after that is treated as theft and revokes that device only.
- Failed logins are rate limited with growing lockouts.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .crypto import Keys, seal, unseal
from .db import Database, utcnow

ACCESS_TTL_SECONDS = 15 * 60
REFRESH_GRACE_SECONDS = 60
MIN_PASSWORD_LENGTH = 12
ISSUER = "HomeFinance"


class AuthError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}


INVALID = "Invalid credentials"


class LoginLimiter:
    """Counts failures per key. After `max_failures`, locks the key with a doubling delay."""

    def __init__(
        self,
        max_failures: int,
        base_delay: float = 30.0,
        max_delay: float = 900.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_failures = max_failures
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.clock = clock
        self._state: dict[str, tuple[int, float]] = {}

    def check(self, key: str) -> None:
        failures, locked_until = self._state.get(key, (0, 0.0))
        now = self.clock()
        if locked_until > now:
            retry = int(locked_until - now) + 1
            raise AuthError(
                429, "Too many attempts. Try again later.", {"Retry-After": str(retry)}
            )

    def failure(self, key: str) -> None:
        failures, _ = self._state.get(key, (0, 0.0))
        failures += 1
        locked_until = 0.0
        if failures >= self.max_failures:
            delay = min(self.base_delay * 2 ** (failures - self.max_failures), self.max_delay)
            locked_until = self.clock() + delay
        self._state[key] = (failures, locked_until)
        if len(self._state) > 10_000:  # bound memory under a flood of distinct keys
            self._state.clear()

    def success(self, key: str) -> None:
        self._state.pop(key, None)


@dataclass(frozen=True)
class AuthContext:
    user_id: int
    session_id: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


class AuthService:
    def __init__(
        self,
        db: Database,
        keys: Keys,
        *,
        clock: Callable[[], float] = time.time,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self.db = db
        self.keys = keys
        self.clock = clock
        self.hasher = hasher or PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
        self._dummy_hash = self.hasher.hash(self._prehash("not-a-real-password"))
        self.user_limiter = LoginLimiter(max_failures=5, clock=clock)
        self.ip_limiter = LoginLimiter(max_failures=20, clock=clock)

    # -- passwords ---------------------------------------------------------

    def _prehash(self, password: str) -> str:
        return hmac.new(self.keys.pepper, password.encode(), hashlib.sha256).hexdigest()

    def _verify_password(self, stored: str | None, password: str) -> bool:
        target = stored or self._dummy_hash  # same work whether or not the user exists
        try:
            ok = self.hasher.verify(target, self._prehash(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        return ok and stored is not None

    # -- user setup --------------------------------------------------------

    def has_user(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is not None

    def create_user(self, username: str, password: str) -> tuple[str, str, list[str]]:
        """Create the one account. Returns (totp_secret, otpauth_uri, recovery_codes)."""
        username = username.strip().lower()
        if not username:
            raise ValueError("Username is required.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
        if self.has_user():
            raise ValueError("A user already exists. This server is built for one user.")
        secret = pyotp.random_base32()
        sealed = seal(secret.encode(), self.keys.kek, b"totp:" + username.encode())
        codes = ["-".join(secrets.token_hex(2) for _ in range(5)) for _ in range(10)]
        with self.db.transaction():
            user_id = self.db.run(
                "INSERT INTO users(username, password_hash, totp_secret, created_at) "
                "VALUES (?,?,?,?)",
                (username, self.hasher.hash(self._prehash(password)), sealed, utcnow()),
            )
            for code in codes:
                self.db.run(
                    "INSERT INTO recovery_codes(user_id, code_hash) VALUES (?,?)",
                    (user_id, _sha256(_normalize_recovery(code))),
                )
        uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)
        return secret, uri, codes

    # -- second factor -----------------------------------------------------

    def _check_totp(self, user, code: str) -> bool:
        if not (code.isdigit() and len(code) == 6):
            return False
        secret = unseal(user["totp_secret"], self.keys.kek, b"totp:" + user["username"].encode())
        totp = pyotp.TOTP(secret.decode())
        step = int(self.clock() // 30)
        for candidate in (step - 1, step, step + 1):
            if candidate <= user["totp_last_step"]:
                continue  # a code can only be used once, and never an older one
            if hmac.compare_digest(totp.at(candidate * 30), code):
                changed = self.db.run(
                    "UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step<?",
                    (candidate, user["id"], candidate),
                )
                return changed == 1
        return False

    def _check_recovery(self, user, code: str) -> bool:
        digest = _sha256(_normalize_recovery(code))
        changed = self.db.run(
            "UPDATE recovery_codes SET used_at=? "
            "WHERE user_id=? AND code_hash=? AND used_at IS NULL",
            (utcnow(), user["id"], digest),
        )
        return changed == 1

    # -- login / tokens ----------------------------------------------------

    def login(
        self, username: str, password: str, code: str, device_name: str, ip: str = "-"
    ) -> dict:
        key = username.strip().lower()
        self.user_limiter.check(key)
        self.ip_limiter.check(ip)
        user = self.db.one("SELECT * FROM users WHERE username=?", (key,))
        ok = self._verify_password(user["password_hash"] if user else None, password)
        code = code.strip()
        if ok and user is not None:
            ok = self._check_totp(user, code) or self._check_recovery(user, code)
        if not ok or user is None:
            self.user_limiter.failure(key)
            self.ip_limiter.failure(ip)
            raise AuthError(401, INVALID)
        self.user_limiter.success(key)
        if self.hasher.check_needs_rehash(user["password_hash"]):
            self.db.run(
                "UPDATE users SET password_hash=? WHERE id=?",
                (self.hasher.hash(self._prehash(password)), user["id"]),
            )
        session_id = uuid.uuid4().hex
        refresh = secrets.token_urlsafe(32)
        now = utcnow()
        self.db.run(
            "INSERT INTO sessions(id, user_id, device_name, refresh_hash, created_at, last_used_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, user["id"], (device_name or "Unknown device")[:80], _sha256(refresh), now, now),
        )
        return self._tokens(user["id"], session_id, refresh)

    def refresh(self, refresh_token: str) -> dict:
        digest = _sha256(refresh_token)
        now = self.clock()
        stolen = False
        with self.db.transaction():
            row = self.db.one(
                "SELECT * FROM sessions WHERE refresh_hash=? OR prev_refresh_hash=?",
                (digest, digest),
            )
            if row is None or row["revoked_at"] is not None:
                raise AuthError(401, INVALID)
            if row["refresh_hash"] == digest:
                previous = digest
            elif now <= (row["prev_valid_until"] or 0):
                # Retry of a rotation whose response was lost: hand out a fresh token.
                previous = row["refresh_hash"]
            else:
                # An old token came back after the grace period: treat it as stolen and
                # revoke this device. The raise happens after the commit, or the
                # transaction would roll the revoke back.
                self.db.run("UPDATE sessions SET revoked_at=? WHERE id=?", (utcnow(), row["id"]))
                stolen = True
            if not stolen:
                new_refresh = secrets.token_urlsafe(32)
                self.db.run(
                    "UPDATE sessions SET refresh_hash=?, prev_refresh_hash=?, prev_valid_until=?, "
                    "last_used_at=? WHERE id=?",
                    (_sha256(new_refresh), previous, now + REFRESH_GRACE_SECONDS, utcnow(), row["id"]),
                )
        if stolen:
            raise AuthError(401, INVALID)
        return self._tokens(row["user_id"], row["id"], new_refresh)

    def _tokens(self, user_id: int, session_id: str, refresh: str) -> dict:
        now = int(self.clock())
        access = jwt.encode(
            {
                "iss": ISSUER,
                "sub": str(user_id),
                "sid": session_id,
                "iat": now,
                "exp": now + ACCESS_TTL_SECONDS,
                "typ": "access",
            },
            self.keys.jwt,
            algorithm="HS256",
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": ACCESS_TTL_SECONDS,
            "session_id": session_id,
        }

    def authenticate(self, access_token: str) -> AuthContext:
        try:
            claims = jwt.decode(
                access_token,
                self.keys.jwt,
                algorithms=["HS256"],
                issuer=ISSUER,
                # Expiry is checked below against our own clock, so tests can move time.
                options={"require": ["exp", "iat", "sub", "sid"], "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            raise AuthError(401, "Invalid or missing credentials") from None
        if claims.get("typ") != "access" or float(claims["exp"]) < self.clock() - 5:
            raise AuthError(401, "Invalid or missing credentials")
        row = self.db.one(
            "SELECT revoked_at FROM sessions WHERE id=? AND user_id=?",
            (claims["sid"], int(claims["sub"])),
        )
        if row is None or row["revoked_at"] is not None:
            raise AuthError(401, "Invalid or missing credentials")
        return AuthContext(user_id=int(claims["sub"]), session_id=claims["sid"])

    def revoke_refresh(self, refresh_token: str) -> None:
        """Sign out the device that holds this refresh token (used by the web page's cookie)."""
        digest = _sha256(refresh_token)
        self.db.run(
            "UPDATE sessions SET revoked_at=? "
            "WHERE (refresh_hash=? OR prev_refresh_hash=?) AND revoked_at IS NULL",
            (utcnow(), digest, digest),
        )

    # -- devices -----------------------------------------------------------

    def sessions(self, ctx: AuthContext) -> list[dict]:
        rows = self.db.query(
            "SELECT id, device_name, created_at, last_used_at FROM sessions "
            "WHERE user_id=? AND revoked_at IS NULL ORDER BY last_used_at DESC",
            (ctx.user_id,),
        )
        return [
            {
                "id": r["id"],
                "device_name": r["device_name"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
                "current": r["id"] == ctx.session_id,
            }
            for r in rows
        ]

    def revoke(self, ctx: AuthContext, session_id: str) -> bool:
        return (
            self.db.run(
                "UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (utcnow(), session_id, ctx.user_id),
            )
            == 1
        )
