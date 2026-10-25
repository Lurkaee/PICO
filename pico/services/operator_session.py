"""PICO Local Operator Session & Pairing Service.

Provides short-lived one-time pairing codes and secure local operator sessions.
Ensures consequential approval actions can be authorized by a verified local operator
without ever exposing server-side secrets (like APPROVAL_SECRET_TOKEN) to frontend bundles,
local storage, or client-side JavaScript.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Optional
from urllib.parse import urlparse

from pico.core.errors import AuthorizationError


ALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "testserver",
    "localhost:8000",
    "127.0.0.1:8000",
    "localhost:5173",
    "127.0.0.1:5173",
    "localhost:3000",
    "127.0.0.1:3000",
}



@dataclass
class PairingCode:
    code: str
    expires_at: datetime
    approver: str = "human_operator"


@dataclass
class OperatorSession:
    session_id: str
    approver: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    is_active: bool = True

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class OperatorSessionService:
    _instance: Optional["OperatorSessionService"] = None

    def __init__(self):
        self._active_codes: dict[str, PairingCode] = {}
        self._sessions: dict[str, OperatorSession] = {}

    @classmethod
    def get_instance(cls) -> "OperatorSessionService":
        if cls._instance is None:
            cls._instance = OperatorSessionService()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """For testing purposes."""
        cls._instance = None

    def generate_pairing_code(self, approver: str = "human_operator", ttl_seconds: int = 300) -> PairingCode:
        # Generate 6-digit code securely
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        pairing = PairingCode(code=code, expires_at=expires_at, approver=approver)
        # Clean expired codes
        now = datetime.now(timezone.utc)
        self._active_codes = {c: p for c, p in self._active_codes.items() if p.expires_at > now}
        self._active_codes[code] = pairing
        return pairing

    def get_active_pairing_code(self) -> Optional[PairingCode]:
        """Returns the most recent valid active pairing code if one exists."""
        now = datetime.now(timezone.utc)
        valid = [p for p in self._active_codes.values() if p.expires_at > now]
        if not valid:
            return None
        return sorted(valid, key=lambda p: p.expires_at, reverse=True)[0]

    def pair(self, code: str, approver: Optional[str] = None, session_ttl_seconds: int = 3600) -> OperatorSession:
        now = datetime.now(timezone.utc)
        clean_code = (code or "").strip()
        pairing = self._active_codes.get(clean_code)

        if not pairing or pairing.expires_at <= now:
            raise AuthorizationError("Invalid or expired pairing code.")

        # Consume code (one-time use)
        del self._active_codes[clean_code]

        effective_approver = approver or pairing.approver or "human_operator"
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=session_ttl_seconds)

        session = OperatorSession(
            session_id=session_id,
            approver=effective_approver,
            csrf_token=csrf_token,
            created_at=now,
            expires_at=expires_at,
            is_active=True,
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: Optional[str]) -> Optional[OperatorSession]:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if not session:
            return None
        if session.is_expired() or not session.is_active:
            self._sessions.pop(session_id, None)
            return None
        return session

    def invalidate_session(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def verify_csrf(self, session: OperatorSession, candidate_csrf_token: Optional[str]) -> bool:
        if not candidate_csrf_token:
            return False
        return secrets.compare_digest(session.csrf_token, candidate_csrf_token)

    @staticmethod
    def validate_origin_and_host(host: Optional[str], origin: Optional[str], referer: Optional[str] = None) -> bool:
        allowed = ("localhost", "127.0.0.1", "testserver")
        # Check host header
        if host:
            host_clean = host.lower().strip()
            # Allow port variants on localhost / 127.0.0.1
            host_domain = host_clean.split(":")[0]
            if host_domain not in allowed:
                return False

        # If origin header is present, must be localhost or 127.0.0.1 or testserver
        if origin:
            try:
                parsed = urlparse(origin)
                hostname = (parsed.hostname or "").lower()
                if hostname not in allowed:
                    return False
            except Exception:
                return False

        # If referer is present and origin is not, check referer
        elif referer:
            try:
                parsed = urlparse(referer)
                hostname = (parsed.hostname or "").lower()
                if hostname not in allowed:
                    return False
            except Exception:
                return False

        return True
