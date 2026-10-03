# app/errors.py
"""
Uniform error contract (audit C8).

Every error body keeps the `detail` field exactly as before (the frontend and any scripts
read it) and gains an `error` object:

    {"detail": <unchanged>,
     "error": {"code": "not_found", "message": "...", "request_id": "..."}}

Also:
  * 422 validation entries no longer echo the submitted `input` back (it could contain a
    password or token the caller just typed) nor internal `ctx`/`url` fields.
  * An unhandled exception returns a JSON 500 with only a generic message and the request id;
    the traceback goes to the log under that id, never to the client.
"""
import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.request_context import get_request_id

logger = logging.getLogger(__name__)

_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 413: "payload_too_large",
    422: "validation_error", 429: "rate_limited", 500: "internal_error",
    502: "bad_gateway", 503: "unavailable", 504: "gateway_timeout",
}


def error_code(status: int) -> str:
    return _CODES.get(status, f"http_{status}")


def _rid(request: Request):
    return getattr(request.state, "request_id", None) or get_request_id()


def build_body(status: int, detail, message: str, request_id) -> dict:
    return {"detail": detail,
            "error": {"code": error_code(status), "message": message, "request_id": request_id}}


def clean_validation_errors(errors) -> list:
    """Keep type/loc/msg only."""
    return [{k: e.get(k) for k in ("type", "loc", "msg") if k in e} for e in errors]


def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    message = detail if isinstance(detail, str) else error_code(exc.status_code).replace("_", " ")
    return JSONResponse(
        status_code=exc.status_code,
        content=build_body(exc.status_code, detail, message, _rid(request)),
        headers=getattr(exc, "headers", None),
    )


def _validation_exception_handler(request: Request, exc: RequestValidationError):
    detail = clean_validation_errors(exc.errors())
    return JSONResponse(
        status_code=422,
        content=build_body(422, detail, "Request validation failed", _rid(request)),
    )


def _unhandled_exception_handler(request: Request, exc: Exception):
    rid = _rid(request)
    logger.error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path,
                 exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(
        status_code=500,
        content=build_body(500, "Internal server error", "Internal server error", rid),
        headers={"X-Request-ID": rid} if rid else None,
    )


def install(app) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
