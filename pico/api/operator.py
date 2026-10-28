"""API endpoints for local operator pairing, session lifecycle, and CSRF protection."""

from typing import Optional
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.orm import Session

from pico.core.errors import AuthorizationError
from pico.db.session import get_db
from pico.schemas.operator import (
    OperatorSessionResponse,
    PairRequest,
    PairResponse,
)
from pico.services.operator_session import OperatorSession, OperatorSessionService

router = APIRouter(prefix="/api/operator", tags=["Operator Session"])

COOKIE_NAME = "pico_operator_session"


def get_operator_session_optional(
    request: Request,
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> Optional[OperatorSession]:
    """Dependency to retrieve and validate an operator session if present."""
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
    client_host = request.client.host if request.client else "unknown"
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
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
