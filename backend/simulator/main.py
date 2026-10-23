import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from simulator.config import SimulatorSettings, get_simulator_settings
from simulator.models import fulfillment_events, idempotency_records, tickets
from simulator.service import BusinessError, RetryRequest, bound_order, retry_fulfillment


def create_simulator_app(
    settings: SimulatorSettings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or get_simulator_settings()
    settings.require_tokens()
    engine = create_engine(
        settings.simulator_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
    )
    clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="TicketPilot Simulator", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    def auth(authorization: str | None, write: bool = False):
        prefix = "Bearer "
        token = (
            authorization[len(prefix) :]
            if authorization and authorization.startswith(prefix)
            else ""
        )
        # Compare UTF-8 bytes to safely reject non-ASCII token input too.
        write_match = secrets.compare_digest(
            token.encode(), settings.simulator_write_token.get_secret_value().encode()
        )
        read_match = secrets.compare_digest(
            token.encode(), settings.simulator_read_token.get_secret_value().encode()
        )
        if not (write_match or (not write and read_match)):
            raise HTTPException(403, "forbidden")

    def read_auth(authorization: str | None = Header(default=None)):
        auth(authorization)

    def write_auth(authorization: str | None = Header(default=None)):
        auth(authorization, write=True)

    @app.exception_handler(BusinessError)
    async def business_error(_: Request, exc: BusinessError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Pydantic default errors include submitted input; never echo it.
        return JSONResponse(status_code=422, content={"error": "INVALID_REQUEST"})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_: Request, exc: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"error": "DATABASE_UNAVAILABLE"})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/internal/tickets", dependencies=[Depends(read_auth)])
    def list_tickets(
        x_tenant_id: str = Header(..., min_length=1, max_length=64),
        after_id: str = Query("", max_length=64, pattern=r"^[A-Za-z0-9_-]*$"),
        limit: int = Query(25, ge=1, le=25),
    ):
        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(
                        tickets.c.tenant_id,
                        tickets.c.id,
                        tickets.c.subject,
                        tickets.c.bound_order_id,
                        tickets.c.status,
                    )
                    .where(tickets.c.tenant_id == x_tenant_id, tickets.c.id > after_id)
                    .order_by(tickets.c.id)
                    .limit(limit + 1)
                ).mappings()
            )
            return {
                "tickets": [dict(row) for row in rows[:limit]],
                "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @app.get("/internal/tickets/{ticket_id}", dependencies=[Depends(read_auth)])
    def get_ticket(ticket_id: str, x_tenant_id: str = Header(..., min_length=1, max_length=64)):
        with engine.connect() as conn:
            row = (
                conn.execute(
                    select(tickets).where(
                        tickets.c.tenant_id == x_tenant_id,
                        tickets.c.id == ticket_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "not found")
        return dict(row)

    @app.get("/internal/orders/{order_id}", dependencies=[Depends(read_auth)])
    def get_order(order_id: str, x_tenant_id: str = Header(...), x_ticket_id: str = Header(...)):
        with engine.connect() as conn:
            row = bound_order(conn, x_tenant_id, x_ticket_id, order_id)
        return dict(row)

    @app.get("/internal/orders/{order_id}/fulfillment-events", dependencies=[Depends(read_auth)])
    def get_events(
        order_id: str,
        x_tenant_id: str = Header(...),
        x_ticket_id: str = Header(...),
        limit: int = 10,
    ):
        if not 1 <= limit <= 20:
            raise HTTPException(422, "limit must be between 1 and 20")
        with engine.connect() as conn:
            bound_order(conn, x_tenant_id, x_ticket_id, order_id)
            rows = conn.execute(
                select(fulfillment_events)
                .where(
                    fulfillment_events.c.tenant_id == x_tenant_id,
                    fulfillment_events.c.order_id == order_id,
                )
                .order_by(fulfillment_events.c.created_at.desc(), fulfillment_events.c.id.desc())
                .limit(limit)
            ).mappings()
            return {"events": [dict(row) for row in rows]}

    @app.post("/internal/orders/{order_id}/retry-fulfillment", dependencies=[Depends(write_auth)])
    def retry(
        order_id: str,
        request: RetryRequest,
        x_tenant_id: str = Header(..., min_length=1, max_length=64),
        x_ticket_id: str = Header(..., min_length=1, max_length=64),
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=240),
    ):
        return retry_fulfillment(
            engine, x_tenant_id, x_ticket_id, order_id, idempotency_key, request, clock()
        )

    @app.get("/internal/operations/{idempotency_key}", dependencies=[Depends(read_auth)])
    def get_operation(
        idempotency_key: str, x_tenant_id: str = Header(...), x_ticket_id: str = Header(...)
    ):
        with engine.connect() as conn:
            row = (
                conn.execute(
                    select(idempotency_records).where(
                        idempotency_records.c.tenant_id == x_tenant_id,
                        idempotency_records.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "not found")
            bound_order(conn, x_tenant_id, x_ticket_id, row["order_id"])
            return row["response_json"]

    return app
