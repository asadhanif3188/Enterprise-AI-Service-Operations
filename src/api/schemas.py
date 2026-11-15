"""Request and response shapes for the local case API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# Field names the create body may contain; anything else is rejected.
CASE_CREATE_FIELDS = ("organization_id", "title", "description")


class CaseCreateRequest(BaseModel):
    """``POST /cases`` body. Unknown fields (``actor_id``, ``status``, ``version``,
    timestamps, ...) are rejected, never ignored.

    Values are strict strings here; the authorized layer checks the canonical
    UUID form, blank text, invalid characters, and length after authentication.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    title: str
    description: str


class CaseResponse(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    description: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class CaseListItem(BaseModel):
    """List entries omit the description."""

    id: UUID
    organization_id: UUID
    title: str
    status: str
    version: int
    created_at: datetime


class CaseListResponse(BaseModel):
    """Interim list shape: cursor pagination is not implemented, so the response
    reports only whether more rows exist (``has_more``) and has no cursor."""

    items: list[CaseListItem]
    has_more: bool
