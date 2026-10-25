"""Pydantic schemas for operator session and pairing endpoints."""

from typing import Optional
from pydantic import BaseModel, Field


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: str
    approver: str


class PairRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=12, description="One-time operator pairing code")
    approver: Optional[str] = Field(default="human_operator", description="Operator identity name")


class PairResponse(BaseModel):
    status: str
    csrf_token: str
    expires_at: str
    approver: str


class OperatorSessionResponse(BaseModel):
    paired: bool
    approver: Optional[str] = None
    expires_at: Optional[str] = None
    csrf_token: Optional[str] = None
