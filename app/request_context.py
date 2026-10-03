# app/request_context.py
"""
Per-request correlation id (audit C8): one id that appears in the X-Request-ID response
header, in every error body, in log lines emitted while the request runs, and in the
audit_logs row the request writes -- so "this 500 the user screenshotted" can be found in
journalctl and in the audit trail with a single grep.

The id is generated server-side. A client-supplied X-Request-ID is only honoured when it is
a short token of [A-Za-z0-9._-] (so a caller can correlate its own logs), never anything
that could carry newlines or markup into logs.
"""
import contextvars
import logging
import re
import uuid

_request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id(incoming=None) -> str:
    if incoming and _SAFE_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


def set_request_id(value) -> None:
    _request_id.set(value)


def get_request_id():
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Adds %(request_id)s to every record ('-' outside a request, e.g. the collector)."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


def install_log_filter() -> None:
    """Attach the filter to every root handler and switch to a format that prints the id."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
        handler.setFormatter(fmt)


async def request_id_middleware(request, call_next):
    """Outermost HTTP middleware: give every request an id and expose it on the response."""
    rid = new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = rid
    set_request_id(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response
