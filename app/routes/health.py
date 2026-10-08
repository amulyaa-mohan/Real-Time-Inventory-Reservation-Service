import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import check_db

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    status = {"db": "ok", "redis": "ok"}

    try:
        await check_db(request.app.state.engine)
    except Exception:
        log.exception("health: database check failed")
        status["db"] = "error"

    try:
        await request.app.state.redis.ping()
    except Exception:
        log.exception("health: redis check failed")
        status["redis"] = "error"

    ok = all(v == "ok" for v in status.values())
    return JSONResponse(status, status_code=200 if ok else 503)
