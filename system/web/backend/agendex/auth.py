"""Optional single-account sessions for the HTTPS deployment.

Credentials remain in server configuration. Only a signed, expiring session is
sent to the browser; the existing local workspace remains usable without login.
"""

import base64
from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from pydantic import Field

from .schemas import InputModel


COOKIE_NAME = "agendex_session"
SESSION_SECONDS = 12 * 60 * 60
SCRYPT_N = 32768


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def hash_password(password: str) -> str:
    """Create the deployment hash without storing a plaintext password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return "$".join(("scrypt", str(SCRYPT_N), "8", "1", _encode(salt), _encode(digest)))


def _password_parts(value: str) -> tuple[bytes, bytes]:
    try:
        kind, n, r, p, salt, digest = value.split("$")
        salt, digest = _decode(salt), _decode(digest)
        if (kind, n, r, p) != ("scrypt", str(SCRYPT_N), "8", "1") or len(salt) != 16 or len(digest) != 32:
            raise ValueError()
        return salt, digest
    except (ValueError, TypeError):
        raise ValueError("AGENDEX_AUTH_PASSWORD_HASH 格式无效") from None


def canonical_origin(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path not in {"", "/"}
                or parts.query or parts.fragment or any(character.isspace() for character in value)):
            return None
        host = parts.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        return "https://" + host + (":" + str(parts.port) if parts.port not in {None, 443} else "")
    except ValueError:
        return None


class LoginInput(InputModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    username: str = ""
    password_hash: str = ""
    secret: str = ""
    origin: str = ""

    @classmethod
    def from_environment(cls):
        flag = os.environ.get("AGENDEX_AUTH_ENABLED", "0").lower()
        if flag not in {"0", "false", "no", "1", "true", "yes"}:
            raise ValueError("AGENDEX_AUTH_ENABLED 必须为 0 或 1")
        enabled = flag in {"1", "true", "yes"}
        if not enabled:
            if os.environ.get("AGENDEX_PUBLIC_ORIGIN"):
                raise ValueError("公网部署必须启用账号登录")
            return cls(False)
        username = os.environ.get("AGENDEX_AUTH_USERNAME", "")
        password_hash = os.environ.get("AGENDEX_AUTH_PASSWORD_HASH", "")
        secret = os.environ.get("AGENDEX_SESSION_SECRET", "")
        origin = canonical_origin(os.environ.get("AGENDEX_PUBLIC_ORIGIN", ""))
        if not username or len(username) > 120 or len(secret) < 32 or origin is None:
            raise ValueError("公网登录配置不完整：请设置账号、密码哈希、会话密钥和 HTTPS 访问地址")
        _password_parts(password_hash)
        return cls(True, username, password_hash, secret, origin)


class AuthManager:
    def __init__(self, config: AuthConfig):
        self.config = config
        self._attempts = OrderedDict()
        self._lock = threading.Lock()

    def trusted_request(self, request) -> bool:
        return canonical_origin(request.url.scheme + "://" + request.headers.get("host", "")) == self.config.origin

    def trusted_origin(self, origin: str | None) -> bool:
        return origin is not None and canonical_origin(origin) == self.config.origin

    def session(self, token: str | None) -> dict:
        if not self.config.enabled:
            return {"enabled": False, "authenticated": True, "username": None}
        username = None
        if token and len(token) <= 2048:
            try:
                encoded, signature = token.split(".")
                expected = hmac.new(self.config.secret.encode(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, expected):
                    data = json.loads(_decode(encoded))
                    timestamp = time.time()
                    if (isinstance(data, dict) and data.get("sub") == self.config.username
                            and type(data.get("exp")) is int and timestamp < data["exp"] <= timestamp + SESSION_SECONDS
                            and isinstance(data.get("nonce"), str)):
                        username = self.config.username
            except (ValueError, TypeError, UnicodeError):
                pass
        return {"enabled": True, "authenticated": username is not None, "username": username}

    def allow_attempt(self, address: str) -> bool:
        """Bound per-client failed-login work without persisting addresses."""
        timestamp = time.monotonic()
        with self._lock:
            attempts = self._attempts.setdefault(address, deque())
            self._attempts.move_to_end(address)
            while attempts and attempts[0] < timestamp - 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return False
            attempts.append(timestamp)
            while len(self._attempts) > 1024:
                self._attempts.popitem(last=False)
            return True

    def verify_credentials(self, username: str, password: str) -> bool:
        salt, expected = _password_parts(self.config.password_hash)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                                r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        user_matches = hmac.compare_digest(username.encode("utf-8"), self.config.username.encode("utf-8"))
        return hmac.compare_digest(actual, expected) and user_matches

    def issue_session(self, response, address: str) -> dict:
        with self._lock:
            self._attempts.pop(address, None)
        data = {"sub": self.config.username, "exp": int(time.time()) + SESSION_SECONDS, "nonce": secrets.token_urlsafe(16)}
        encoded = _encode(json.dumps(data, separators=(",", ":")).encode())
        signature = hmac.new(self.config.secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(COOKIE_NAME, encoded + "." + signature, max_age=SESSION_SECONDS,
                            httponly=True, secure=True, samesite="lax", path="/")
        return {"enabled": True, "authenticated": True, "username": self.config.username}

    @staticmethod
    def clear_session(response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


def protected_path(path: str) -> bool:
    if path in {"/api/auth/session", "/api/auth/login", "/api/auth/logout"}:
        return False
    return path == "/api" or path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"} or path.startswith("/docs/")
