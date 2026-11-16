"""Safe HTTP error envelope and the single mapping from domain outcomes to HTTP.

Every error response has the shape ``{"error": {"code": ..., "message": ...}}``
with fixed, server-written text. Responses never contain exception messages,
SQL, membership details, case text, credentials, or client-supplied values.
Server errors add a server-generated ``request_id`` (also sent as the
``X-Request-ID`` header on every response) for correlating logs.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from src.case_authorization import (
    CaseAccessError,
    CaseCreationOutcomeUnknown,
    CaseNotFound,
    CaseOperationUnavailable,
    IdempotencyPayloadMismatch,
    IdempotencyRequestInFlight,
    InvalidCaseRequest,
    OrganizationAccessDenied,
)
from src.credential_auth import (
    AuthenticationError,
    AuthenticationUnavailable,
    InactiveActor,
    Unauthenticated,
)

logger = logging.getLogger("eaiso.http")

REQUEST_ID_HEADER = "X-Request-ID"

# Fixed messages for layer input-validation reasons (no client values).
_INVALID_MESSAGES = {
    "organization_id_malformed": "organization_id must be a canonical UUID",
    "case_id_malformed": "Case ID must be a canonical UUID",
    "title_blank": "title must be non-blank text",
    "title_too_long": "title is too long",
    "title_invalid_character": "title contains an invalid character",
    "description_blank": "description must be non-blank text",
    "description_too_long": "description is too long",
    "description_invalid_character": "description contains an invalid character",
    "limit_out_of_range": "limit must be between 1 and 100",
    "idempotency_key_malformed": "Idempotency-Key is invalid",
}


class ApiError(Exception):
    """An HTTP-level rejection raised by this package before or around the layer."""

    def __init__(self, status: int, code: str, message: str, *,
                 details: list[dict] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def request_id(request: Request) -> str:
    return str(request.state.request_id) if hasattr(request.state, "request_id") else "unknown"


def error_response(request: Request, status: int, code: str, message: str, *,
                   details: list[dict] | None = None, headers: dict | None = None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if details:
        body["details"] = details
    if status >= 500:
        body["request_id"] = request_id(request)
        logger.warning("http.error status=%d code=%s request_id=%s", status, code, request_id(request))
    all_headers = {REQUEST_ID_HEADER: request_id(request)}
    if headers:
        all_headers.update(headers)
    return JSONResponse({"error": body}, status_code=status, headers=all_headers)


def invalid_request(exc: InvalidCaseRequest, *, status: int) -> ApiError:
    """Convert the layer's input-validation outcome (raised after authentication)."""
    code = "invalid_parameter" if status == 400 else "invalid_request"
    return ApiError(status, code, _INVALID_MESSAGES.get(exc.reason, "Invalid request"))


def unauthenticated_response(request: Request) -> JSONResponse:
    # One body for every Unauthenticated reason: missing, malformed, unknown,
    # revoked, and actor_not_found are indistinguishable to the client.
    return error_response(request, 401, "unauthenticated", "Authentication required",
                          headers={"WWW-Authenticate": "Bearer"})


def internal_error_response(request: Request) -> JSONResponse:
    return error_response(request, 500, "internal_error", "Internal server error")


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(request, exc.status, exc.code, exc.message, details=exc.details)


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map authentication and authorization outcomes. Order matters: most specific first."""
    if isinstance(exc, Unauthenticated):
        return unauthenticated_response(request)
    if isinstance(exc, InactiveActor):
        return error_response(request, 403, "actor_inactive", "Actor is not active")
    if isinstance(exc, OrganizationAccessDenied):
        return error_response(request, 403, "organization_access_denied",
                              "Not permitted for this organization")
    if isinstance(exc, CaseNotFound):
        return error_response(request, 404, "case_not_found", "Case not found")
    if isinstance(exc, CaseCreationOutcomeUnknown):
        # Never success and never a claimed rollback. A keyed request resolves
        # the outcome by retrying with the same key; an unkeyed retry may
        # create a duplicate case.
        if request.headers.get("idempotency-key") is not None:
            return error_response(
                request, 500, "case_creation_outcome_unknown",
                "The case may or may not have been created. Retry with the same "
                "Idempotency-Key and payload to learn the outcome.")
        return error_response(
            request, 500, "case_creation_outcome_unknown",
            "The case may or may not have been created. Retrying this request may create "
            "a duplicate case.")
    if isinstance(exc, IdempotencyPayloadMismatch):
        return error_response(request, 422, "idempotency_payload_mismatch",
                              "Idempotency key was already used with a different request")
    if isinstance(exc, IdempotencyRequestInFlight):
        # The competing request's outcome is unknown; this request created nothing.
        return error_response(request, 503, "idempotency_request_in_flight",
                              "A request with this idempotency key is still in progress. "
                              "Retry with the same key.")
    if isinstance(exc, InvalidCaseRequest):
        return error_response(request, 422, "invalid_request",
                              _INVALID_MESSAGES.get(exc.reason, "Invalid request"))
    if isinstance(exc, (CaseOperationUnavailable, AuthenticationUnavailable)):
        return internal_error_response(request)
    if isinstance(exc, (AuthenticationError, CaseAccessError)):
        return internal_error_response(request)  # fail closed for any unmapped outcome
    return internal_error_response(request)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Framework errors (unknown route, wrong method) in the same envelope."""
    status = exc.status_code if hasattr(exc, "status_code") else 500
    if status == 404:
        return error_response(request, 404, "route_not_found", "Not found")
    if status == 405:
        return error_response(request, 405, "method_not_allowed", "Method not allowed")
    if status >= 500:
        return internal_error_response(request)
    return error_response(request, 400, "bad_request", "Bad request")


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Request parsing is done explicitly in this package, so the framework's own
    # validation should not trigger; if it does, its details (which can echo
    # input) are never returned.
    return error_response(request, 400, "bad_request", "Bad request")


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Neither the message nor the traceback is logged here: either could carry
    # request text. The request ID correlates the response with server logs.
    logger.error("http.unhandled request_id=%s", request_id(request))
    return internal_error_response(request)
