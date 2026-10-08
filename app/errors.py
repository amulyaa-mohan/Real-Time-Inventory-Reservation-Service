"""Error codes and handlers.

Every error response has the body {"error": {"code": "<CODE>", "message": "<text>"}}.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    """Raise from routes/services; the handler turns it into the JSON error body."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def not_found(what: str) -> AppError:
    return AppError(404, "NOT_FOUND", f"{what} not found")


def error_response(status_code: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Bad body/params (e.g. negative qty). Keep the field-level detail in the message.
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return error_response(422, "VALIDATION_ERROR", "; ".join(parts))


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Framework errors such as unknown routes (404) or wrong method (405).
    code = "NOT_FOUND" if exc.status_code == 404 else f"HTTP_{exc.status_code}"
    return error_response(exc.status_code, code, str(exc.detail), getattr(exc, "headers", None))


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "INTERNAL_ERROR", "internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)
