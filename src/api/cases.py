"""The three case routes: ``POST /cases``, ``GET /cases/{id}``, ``GET /cases``.

Each route passes the raw bearer credential to the authorized case operations
(``src.authorized_case_operations``), which resolve the credential, apply the
permission policy, and run the scoped database operation. This module holds no
security policy of its own: it parses HTTP and maps typed outcomes.

Request processing order (FastAPI resolves a route's dependencies in the order
they are declared, before the route body runs):

1. ``POST`` only: ``Idempotency-Key`` syntax (ADR-0004). Absent: an ordinary
   unkeyed create. Present: exactly one field instance whose value is an RFC
   9651 String matching ``[A-Za-z0-9._~-]{16,128}``, else ``400``. A malformed
   key never falls back to an unkeyed create. The value is never logged.
2. ``Authorization`` header syntax: exactly one ``Bearer <token>`` value, else
   the generic ``401``. The token is not verified here.
3. Request syntax: query parameters, then (``POST``) content type, JSON, and
   the body schema (``400``, ``415``, ``422``). This runs **before** the
   credential is verified, so a well-formed but invalid credential with a
   malformed body gets ``400``/``415``/``422``, not ``401``.
4. The authorized layer: credential verification (``401``/``403``/``500``),
   then value checks such as the canonical UUID form and blank text, then
   authorization and the scoped operation. For a keyed create, authorization
   always precedes any idempotency-key lookup (ADR-0004).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.api.errors import ApiError, invalid_request
from src.api.schemas import (
    CASE_CREATE_FIELDS,
    CaseCreateRequest,
    CaseListResponse,
    CaseResponse,
)
from src.api.structured_fields import StructuredFieldError, parse_string_item
from src.authorized_case_operations import DEFAULT_LIST_LIMIT
from src.case_authorization import InvalidCaseRequest
from src.credential_auth import Unauthenticated

router = APIRouter()

# RFC 6750 b64token after a case-insensitive "Bearer" and exactly one space.
_BEARER = re.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr] ([A-Za-z0-9._~+/-]+=*)")
_JSON_MEDIA_TYPE = re.compile(r"application/json(\s*;\s*charset=(utf-8|\"utf-8\"))?\s*",
                              re.IGNORECASE)
_LIMIT_SYNTAX = re.compile(r"[0-9]{1,4}")
MAX_BODY_BYTES = 64 * 1024
LIST_PARAMETERS = ("organization_id", "limit")
IDEMPOTENCY_HEADER = "idempotency-key"
# Syntax check only; the authorized layer validates the key again.
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._~-]{16,128}")


# ----- dependencies (run in declaration order) ------------------------------------

def idempotency_key(request: Request) -> str | None:
    """Return the decoded key, ``None`` when no key was sent, or raise ``400``.

    Header names are compared case-insensitively and with ``_`` read as ``-``,
    so a spelling that a proxy might normalize differently is never ignored:
    it is rejected as ambiguous, like duplicate field instances.
    """
    instances = [(name, value) for name, value in request.headers.raw
                 if name.decode("latin-1").lower().replace("_", "-") == IDEMPOTENCY_HEADER]
    if not instances:
        return None
    key: str | None = None
    if len(instances) == 1 and instances[0][0].decode("latin-1").lower() == IDEMPOTENCY_HEADER:
        try:
            key = parse_string_item(instances[0][1].decode("ascii"))
        except (UnicodeDecodeError, StructuredFieldError):
            key = None
    if key is None or _IDEMPOTENCY_KEY.fullmatch(key) is None:
        # Raised outside the except block; the value is never echoed or logged.
        raise ApiError(400, "invalid_idempotency_key",
                       "Idempotency-Key must be one RFC 9651 String of 16 to 128 characters "
                       "from A-Z, a-z, 0-9, '.', '_', '~', and '-'")
    return key


def bearer_credential(request: Request) -> str:
    """Return the raw opaque token. Syntax only: the layer verifies it."""
    values = request.headers.getlist("authorization")
    if not values:
        raise Unauthenticated("missing")
    if len(values) != 1:
        raise Unauthenticated("malformed")  # duplicated or ambiguous header
    match = _BEARER.fullmatch(values[0])
    if match is None:
        raise Unauthenticated("malformed")
    return match.group(1)


def _reject_unexpected_parameters(request: Request, allowed: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for name, _ in request.query_params.multi_items():
        if name == "cursor":
            raise ApiError(400, "cursor_not_supported",
                           "Cursor pagination is not implemented")
        if name not in allowed:
            raise ApiError(400, "unknown_parameter", "Unknown query parameter")
        if name in seen:
            raise ApiError(400, "duplicate_parameter", "Query parameter given more than once")
        seen.add(name)


def no_query_parameters(request: Request) -> None:
    _reject_unexpected_parameters(request, ())


def list_parameters(request: Request) -> tuple[str, int]:
    _reject_unexpected_parameters(request, LIST_PARAMETERS)
    organization_id = request.query_params.get("organization_id")
    if organization_id is None:
        # Never an implicit listing across organizations.
        raise ApiError(400, "missing_parameter", "organization_id is required")
    raw_limit = request.query_params.get("limit")
    if raw_limit is None:
        return organization_id, DEFAULT_LIST_LIMIT
    if _LIMIT_SYNTAX.fullmatch(raw_limit) is None:
        raise ApiError(400, "invalid_parameter", "limit must be an integer")
    return organization_id, int(raw_limit)  # the range is checked by the layer


def _no_duplicate_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


async def case_create_body(request: Request) -> CaseCreateRequest:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or _JSON_MEDIA_TYPE.fullmatch(content_types[0]) is None:
        raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "Request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "Request body is too large")
    data: object = None
    malformed = False
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: deep nesting
        malformed = True
    if malformed:  # raised outside the except block: no chained parser message
        raise ApiError(400, "malformed_json", "Request body is not valid JSON")
    if not isinstance(data, dict):
        raise ApiError(422, "invalid_request", "Request body must be a JSON object")
    details: list[dict] | None = None
    try:
        return CaseCreateRequest.model_validate(data)
    except ValidationError as exc:
        # Field names only for known fields; never input values or unknown names.
        details = [{"field": err["loc"][0] if err["loc"] and err["loc"][0] in CASE_CREATE_FIELDS
                    else None, "problem": err["type"]} for err in exc.errors()]
    raise ApiError(422, "invalid_request", "Request body is invalid", details=details)


def case_operations(request: Request):
    """The authorized operations built once at startup by the trusted factory."""
    return request.app.state.case_operations


# Parameter types: FastAPI resolves these dependencies in parameter order.
IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]
BearerCredential = Annotated[str, Depends(bearer_credential)]
NoQueryParameters = Annotated[None, Depends(no_query_parameters)]
CreateBody = Annotated[CaseCreateRequest, Depends(case_create_body)]
ListSelection = Annotated[tuple[str, int], Depends(list_parameters)]
# Typed loosely on purpose: route modules may not import the layer class.
Operations = Annotated[Any, Depends(case_operations)]


# ----- routes ---------------------------------------------------------------------

@router.post("/cases", status_code=201, response_model=CaseResponse)
def create_case(
    response: Response,
    key: IdempotencyKey,
    credential: BearerCredential,
    _query: NoQueryParameters,
    body: CreateBody,
    operations: Operations,
) -> Any:
    """Create a case. Without a key, separate successful requests create separate
    cases. With a key (ADR-0004): ``201`` for a new case, ``200`` identity-only
    replay, ``422`` payload mismatch, or ``503`` if a concurrent request with the
    same scope stays unresolved.
    """
    rejected: InvalidCaseRequest | None = None
    try:
        if key is None:
            created = operations.create_case(credential, body.organization_id, body.title,
                                             body.description)
            replayed = False
        else:
            outcome = operations.create_case_keyed(credential, body.organization_id, body.title,
                                                   body.description, key)
            created, replayed = outcome.representation, outcome.replayed
    except InvalidCaseRequest as exc:
        rejected = exc
    if rejected is not None:
        raise invalid_request(rejected, status=400 if rejected.reason == "idempotency_key_malformed"
                              else 422)
    location = f"/cases/{created['id']}"
    if replayed:
        # Identity only, from the idempotency record. Returned as a response
        # object so the full CaseResponse model neither expands nor rejects it.
        return JSONResponse({"id": str(created["id"]),
                             "organization_id": str(created["organization_id"])},
                            status_code=200,
                            headers={"Location": location, "Idempotency-Replayed": "true"})
    response.headers["Location"] = location
    return created


@router.get("/cases/{case_id}", response_model=CaseResponse)
def get_case(
    case_id: str,
    credential: BearerCredential,
    _query: NoQueryParameters,
    operations: Operations,
) -> dict:
    """Ownership comes from the stored case; no header or claim selects it."""
    rejected: InvalidCaseRequest | None = None
    try:
        return operations.get_case(credential, case_id)
    except InvalidCaseRequest as exc:
        rejected = exc
    assert rejected is not None
    raise invalid_request(rejected, status=400)


@router.get("/cases", response_model=CaseListResponse)
def list_cases(
    credential: BearerCredential,
    selection: ListSelection,
    operations: Operations,
) -> dict:
    organization_id, limit = selection
    rejected: InvalidCaseRequest | None = None
    try:
        return operations.list_cases(credential, organization_id, limit=limit)
    except InvalidCaseRequest as exc:
        rejected = exc
    assert rejected is not None
    raise invalid_request(rejected, status=400)
