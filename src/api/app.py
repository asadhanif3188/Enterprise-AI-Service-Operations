"""FastAPI application for the local case API.

Run on the loopback interface only (see ``scripts/run_local_api.py``)::

    python -m scripts.run_local_api            # serves http://127.0.0.1:8000

Configuration is server-owned and read once at startup by the trusted
``operations_from_environment`` factory: ``DATABASE_URL`` and, optionally,
``EAISO_CREDENTIALS_FILE``. Missing or invalid configuration stops startup.

Exactly three routes exist. The interactive documentation and OpenAPI routes
are disabled, and there are no administrative, credential, provisioning, or
status-transition routes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api import errors
from src.api.cases import router as cases_router
from src.authorized_case_operations import operations_from_environment
from src.case_authorization import CaseAccessError
from src.credential_auth import AuthenticationError

logger = logging.getLogger("eaiso.http")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Built once, from server configuration only; no request input reaches it.
    app.state.case_operations = operations_from_environment()
    logger.info("http.startup routes=3")
    yield


def create_app() -> FastAPI:
    # redirect_slashes=False: no redirects built from the client's Host header,
    # and no route-like behavior beyond the three endpoints.
    app = FastAPI(title="EAISO local case API", docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False, lifespan=lifespan)
    app.include_router(cases_router)

    @app.middleware("http")
    async def request_id_and_access_log(request: Request, call_next):
        # Server-generated only: a client-supplied X-Request-ID is ignored.
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers[errors.REQUEST_ID_HEADER] = request.state.request_id
        # Method, path, status, and request ID only: never headers, bodies, or queries.
        logger.info("http.request method=%s path=%s status=%d request_id=%s",
                    request.method, request.url.path, response.status_code,
                    request.state.request_id)
        return response

    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(AuthenticationError, errors.domain_error_handler)
    app.add_exception_handler(CaseAccessError, errors.domain_error_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)
    return app


app = create_app()
