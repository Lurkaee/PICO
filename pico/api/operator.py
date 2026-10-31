"""API endpoints for local operator pairing, session lifecycle, and CSRF protection."""

from dataclasses import dataclass
import secrets
from typing import Optional
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import AuthorizationError
from pico.db.session import get_db
from pico.homeassistant.ws_client import is_loopback_host
from pico.schemas.operator import (
    OperatorSessionResponse,
    PairRequest,
    PairResponse,
)
from pico.services.operator_session import OperatorSession, OperatorSessionService

router = APIRouter(prefix="/api/operator", tags=["Operator Session"])

COOKIE_NAME = "pico_operator_session"


def is_loopback_client(request: Request) -> bool:
    """Determine whether the direct TCP connection originates from local loopback.

    Never trusts forwarded IP headers (e.g. X-Forwarded-For).
    """
    if not request.client or not request.client.host:
        return False
    return is_loopback_host(request.client.host)


def get_operator_session_optional(
    request: Request,
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> Optional[OperatorSession]:
    """Dependency to retrieve and validate an operator session if present.

    Invariants:
    1. A paired operator session is accepted ONLY from genuine local loopback connections.
    2. Non-loopback callers cannot use an operator session cookie to authorize requests.
    3. Host, Origin, and Referer must match trusted local hosts.
    4. Mutating methods (POST, PUT, PATCH, DELETE) require a valid session-bound CSRF token.
    """
    if not is_loopback_client(request):
        return None

    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        return None

    service = OperatorSessionService.get_instance()
    session = service.get_session(session_id, db=db)
    if not session:
        return None

    # Origin and Host validation
    host = request.headers.get("host")
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if not service.validate_origin_and_host(host, origin, referer):
        raise AuthorizationError("Untrusted origin or host for operator session.")

    # Mutating requests require CSRF token
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not service.verify_csrf(session, x_csrf_token):
            raise AuthorizationError("Invalid or missing CSRF token for operator action.")

    return session


def require_operator_session(
    session: Optional[OperatorSession] = Depends(get_operator_session_optional),
) -> OperatorSession:
    """Dependency that strictly requires an active authenticated operator session."""
    if not session:
        raise AuthorizationError("Active local operator session required. Please pair first.")
    return session


@dataclass
class AuthorizationContext:
    is_authorized: bool
    principal: str  # "local_operator", "authenticated_token_holder", "anonymous"
    method: str  # "operator_session", "approval_token", "none"
    session: Optional[OperatorSession] = None
    is_loopback: bool = False


def get_authorization_context(
    request: Request,
    x_approval_token: Optional[str] = Header(default=None, alias="X-Pico-Approval-Token"),
    operator_session: Optional[OperatorSession] = Depends(get_operator_session_optional),
) -> AuthorizationContext:
    """Evaluates caller authorization under Policy A.

    - Local desktop browser on loopback: Valid paired operator session (+ CSRF on mutations).
      Principal: 'local_operator'.
    - Remote or headless client: Valid X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN.
      Principal: 'authenticated_token_holder'.
    - Remote requests presenting only an operator session cookie are strictly rejected.
    """
    is_loopback = is_loopback_client(request)

    # 1. Local paired operator session
    if is_loopback and operator_session is not None:
        return AuthorizationContext(
            is_authorized=True,
            principal="local_operator",
            method="operator_session",
            session=operator_session,
            is_loopback=True,
        )

    # 2. Remote or headless approval token
    configured_secret = (settings.APPROVAL_SECRET_TOKEN or "").strip()
    clean_token = (x_approval_token or "").strip()
    if configured_secret and clean_token and secrets.compare_digest(clean_token, configured_secret):
        return AuthorizationContext(
            is_authorized=True,
            principal="authenticated_token_holder",
            method="approval_token",
            session=None,
            is_loopback=is_loopback,
        )

    # 3. Unauthenticated / unauthorized
    return AuthorizationContext(
        is_authorized=False,
        principal="anonymous",
        method="none",
        session=None,
        is_loopback=is_loopback,
    )


def require_consequential_authorization(
    auth_ctx: AuthorizationContext = Depends(get_authorization_context),
) -> AuthorizationContext:
    """Strictly requires authorization under Policy A for consequential mutations."""
    if not auth_ctx.is_authorized:
        raise AuthorizationError(
            "Consequential operation fails closed without valid local operator session or approval token."
        )
    return auth_ctx


def require_history_authorization(
    auth_ctx: AuthorizationContext = Depends(get_authorization_context),
) -> AuthorizationContext:
    """Strictly requires authorization under Policy A for execution-history and evidence reads."""
    if not auth_ctx.is_authorized:
        raise AuthorizationError(
            "Access to execution history fails closed without valid local operator session or approval token."
        )
    return auth_ctx


@router.post(
    "/pair",
    response_model=PairResponse,
    summary="Exchange a one-time pairing code for an HttpOnly operator session cookie",
)
def pair_operator(
    payload: PairRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> PairResponse:
    # Verify request originates from local loopback
    if not is_loopback_client(request):
        raise AuthorizationError("Operator pairing is restricted to local loopback.")

    # Origin and host validation
    service = OperatorSessionService.get_instance()
    host = request.headers.get("host")
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if not service.validate_origin_and_host(host, origin, referer):
        raise AuthorizationError("Untrusted origin or host for operator pairing.")

    session = service.pair(code=payload.code, db=db)

    # Set secure HttpOnly cookie (strict same-site, local path)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session.session_id,
        httponly=True,
        samesite="strict",
        secure=False,  # Local development over HTTP
        path="/api",
        max_age=3600,
    )

    return PairResponse(
        status="paired",
        csrf_token=session.csrf_token,
        expires_at=session.expires_at.isoformat(),
        approver=session.approver,
    )


@router.get(
    "/session",
    response_model=OperatorSessionResponse,
    summary="Check current operator session state",
)
def check_session(
    request: Request,
    db: Session = Depends(get_db),
) -> OperatorSessionResponse:
    if not is_loopback_client(request):
        return OperatorSessionResponse(paired=False)

    session_id = request.cookies.get(COOKIE_NAME)
    service = OperatorSessionService.get_instance()
    session = service.get_session(session_id, db=db)
    if not session:
        return OperatorSessionResponse(paired=False)

    return OperatorSessionResponse(
        paired=True,
        approver=session.approver,
        expires_at=session.expires_at.isoformat(),
        csrf_token=session.csrf_token,
    )


@router.post(
    "/logout",
    summary="Invalidate active operator session and clear session cookie",
)
def logout_operator(
    request: Request,
    response: Response,
    session: Optional[OperatorSession] = Depends(get_operator_session_optional),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if session:
        OperatorSessionService.get_instance().invalidate_session(session.session_id, db=db)
    response.delete_cookie(key=COOKIE_NAME, path="/api")
    return {"status": "logged_out"}
