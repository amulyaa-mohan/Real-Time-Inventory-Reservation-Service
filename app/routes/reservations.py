from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.errors import AppError
from app.models import ReservationCreate, ReservationOut
from app.services import reservations as svc

router = APIRouter(prefix="/reservations", tags=["reservations"])


@router.post("", status_code=201, response_model=ReservationOut)
async def create_reservation(
    body: ReservationCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: AsyncEngine = Depends(get_engine),
):
    if not idempotency_key or not idempotency_key.strip():
        raise AppError(400, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required")

    reservation, replayed = await svc.reserve(engine, body.product_id, body.qty, idempotency_key)
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return reservation


@router.get("/{reservation_id}", response_model=ReservationOut)
async def get_reservation(reservation_id: UUID, engine: AsyncEngine = Depends(get_engine)):
    return await svc.get_reservation(engine, reservation_id)
