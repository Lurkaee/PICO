"""PICO Local Operator Session & Pairing Service.

Provides short-lived one-time pairing challenges persisted in SQLite and secure local operator sessions.
Ensures consequential approval actions can be authorized by a verified local operator
without ever exposing server-side secrets (like APPROVAL_SECRET_TOKEN) to frontend bundles,
local storage, or client-side JavaScript.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Generator, Optional
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pico.core.errors import AuthorizationError
from pico.db.session import SessionLocal
from pico.models.operator_session import (
    OperatorPairingChallenge,
    OperatorSessionModel,
)

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


LOCAL_OPERATOR_IDENTITY: str = "local_operator"


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class PairingCode:
    code: str
    expires_at: datetime
    approver: str = LOCAL_OPERATOR_IDENTITY


@dataclass
class OperatorSession:
    session_id: str
    approver: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    is_active: bool = True

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= ensure_utc(self.expires_at)


class OperatorSessionService:
    _instance: Optional["OperatorSessionService"] = None

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "OperatorSessionService":
        if cls._instance is None:
            cls._instance = OperatorSessionService()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """For testing purposes."""
        cls._instance = None

    @contextmanager
    def _get_db(self, db: Optional[Session]) -> Generator[Session, None, None]:
        if db is not None:
            yield db
        else:
            session = SessionLocal()
            try:
                yield session
            finally:
                session.close()

    def generate_pairing_code(
        self,
        ttl_seconds: int = 300,
        db: Optional[Session] = None,
    ) -> PairingCode:
        """Generate a 6-digit one-time code and persist its challenge in the database bound to local_operator."""
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        salt = secrets.token_hex(16)
        code_hash = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        challenge_id = f"pair_{secrets.token_hex(16)}"

        with self._get_db(db) as s:
            # Clean up old expired challenges
            expired_q = select(OperatorPairingChallenge).where(
                OperatorPairingChallenge.expires_at < now
            )
            for old in s.scalars(expired_q).all():
                s.delete(old)

            challenge = OperatorPairingChallenge(
                id=challenge_id,
                code_hash=code_hash,
                salt=salt,
                approver=LOCAL_OPERATOR_IDENTITY,
                failed_attempts=0,
                max_attempts=5,
                created_at=now,
                expires_at=expires_at,
                consumed_at=None,
            )
            s.add(challenge)
            s.commit()

        return PairingCode(code=code, expires_at=expires_at, approver=LOCAL_OPERATOR_IDENTITY)

    def pair(
        self,
        code: str,
        session_ttl_seconds: int = 3600,
        db: Optional[Session] = None,
    ) -> OperatorSession:

        """
        Validate pairing code against active unconsumed challenges.
        Atomically consumes the code and creates an authenticated OperatorSession.
        The session's approver identity is bound strictly from the challenge.
        """
        now = datetime.now(timezone.utc)
        clean_code = (code or "").strip()
        if not clean_code:
            raise AuthorizationError("Pairing code cannot be empty.")

        with self._get_db(db) as s:
            # Find active unconsumed challenges
            challenges = s.scalars(
                select(OperatorPairingChallenge)
                .where(
                    OperatorPairingChallenge.consumed_at.is_(None),
                    OperatorPairingChallenge.expires_at > now,
                )
                .order_by(OperatorPairingChallenge.created_at.desc())
            ).all()

            if not challenges:
                raise AuthorizationError("Invalid or expired pairing code.")

            # Check if all active challenges are locked out
            all_locked = all(ch.failed_attempts >= ch.max_attempts for ch in challenges)
            if all_locked:
                raise AuthorizationError(
                    "Pairing code locked due to excessive failed attempts. Please generate a new code in the local terminal."
                )

            # Test code against candidate challenges
            matched_ch: Optional[OperatorPairingChallenge] = None
            for ch in challenges:
                if ch.failed_attempts >= ch.max_attempts:
                    continue
                candidate_hash = hashlib.sha256(f"{ch.salt}:{clean_code}".encode()).hexdigest()
                if secrets.compare_digest(ch.code_hash, candidate_hash):
                    matched_ch = ch
                    break

            if not matched_ch:
                # Increment failed attempts on active non-locked challenges
                for ch in challenges:
                    if ch.failed_attempts < ch.max_attempts:
                        ch.failed_attempts += 1
                s.commit()
                raise AuthorizationError("Invalid or expired pairing code.")

            # Atomically consume the matching challenge
            result = s.execute(
                update(OperatorPairingChallenge)
                .where(
                    OperatorPairingChallenge.id == matched_ch.id,
                    OperatorPairingChallenge.consumed_at.is_(None),
                    OperatorPairingChallenge.failed_attempts < OperatorPairingChallenge.max_attempts,
                )
                .values(consumed_at=now)
            )

            if result.rowcount == 0:
                s.rollback()
                raise AuthorizationError("Pairing code has already been consumed or locked.")

            # Session approver identity is bound to the challenge created by the CLI
            bound_approver = matched_ch.approver
            session_id = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            session_expires_at = now + timedelta(seconds=session_ttl_seconds)

            session_model = OperatorSessionModel(
                id=session_id,
                approver=bound_approver,
                csrf_token=csrf_token,
                created_at=now,
                expires_at=session_expires_at,
                is_active=True,
            )
            s.add(session_model)
            s.commit()

            return OperatorSession(
                session_id=session_id,
                approver=bound_approver,
                csrf_token=csrf_token,
                created_at=now,
                expires_at=session_expires_at,
                is_active=True,
            )

    def get_session(
        self,
        session_id: Optional[str],
        db: Optional[Session] = None,
    ) -> Optional[OperatorSession]:
        """Retrieve and validate an active operator session by ID."""
        if not session_id:
            return None

        now = datetime.now(timezone.utc)
        with self._get_db(db) as s:
            session_model = s.get(OperatorSessionModel, session_id)
            if not session_model:
                return None
            if not session_model.is_active or ensure_utc(session_model.expires_at) <= now:
                if session_model.is_active:
                    session_model.is_active = False
                    s.commit()
                return None

            return OperatorSession(
                session_id=session_model.id,
                approver=session_model.approver,
                csrf_token=session_model.csrf_token,
                created_at=session_model.created_at,
                expires_at=session_model.expires_at,
                is_active=session_model.is_active,
            )

    def invalidate_session(
        self,
        session_id: Optional[str],
        db: Optional[Session] = None,
    ) -> bool:
        """Revoke an active operator session."""
        if not session_id:
            return False

        with self._get_db(db) as s:
            session_model = s.get(OperatorSessionModel, session_id)
            if session_model and session_model.is_active:
                session_model.is_active = False
                s.commit()
                return True
            return False

    def verify_csrf(self, session: OperatorSession, candidate_csrf_token: Optional[str]) -> bool:
        if not candidate_csrf_token:
            return False
        return secrets.compare_digest(session.csrf_token, candidate_csrf_token)

    @staticmethod
    def validate_origin_and_host(
        host: Optional[str],
        origin: Optional[str],
        referer: Optional[str] = None,
    ) -> bool:
        allowed = ("localhost", "127.0.0.1", "testserver")
        if host:
            host_clean = host.lower().strip()
            host_domain = host_clean.split(":")[0]
            if host_domain not in allowed:
                return False

        if origin:
            try:
                parsed = urlparse(origin)
                hostname = (parsed.hostname or "").lower()
                if hostname not in allowed:
                    return False
            except Exception:
                return False
        elif referer:
            try:
                parsed = urlparse(referer)
                hostname = (parsed.hostname or "").lower()
                if hostname not in allowed:
                    return False
            except Exception:
                return False

        return True
