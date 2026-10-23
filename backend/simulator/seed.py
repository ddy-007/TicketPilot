"""Add missing synthetic S01-S06 fixtures; never reset existing business state."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import ConfigurationError
from simulator.config import get_simulator_settings
from simulator.models import fulfillment_events, orders, tickets


def seed_demo(engine, now: datetime) -> None:
    with engine.begin() as conn:
        for index in range(1, 7):
            order_id = f"O{2000 + index}"
            ticket_id = f"T{1000 + index}"
            created = conn.execute(
                insert(orders)
                .values(
                    tenant_id="demo",
                    id=order_id,
                    payment_status="PENDING" if index == 5 else "PAID",
                    fulfillment_status={3: "SHIPPED", 4: "QUEUED"}.get(index, "FAILED"),
                    inventory_reserved=index != 2,
                    paid_at=None if index == 5 else now - timedelta(hours=1),
                    version=3,
                )
                .on_conflict_do_nothing()
                .returning(orders.c.id)
            ).scalar_one_or_none()
            conn.execute(
                insert(tickets)
                .values(
                    tenant_id="demo",
                    id=ticket_id,
                    subject=f"S0{index} 订单未正常进入发货流程",
                    body="合成工单：订单尚未开始发货，请检查当前绑定订单。",
                    bound_order_id=None if index == 6 else order_id,
                    status="OPEN",
                )
                .on_conflict_do_nothing()
            )
            # Evidence is synthetic only for orders created in this same seed transaction.
            # Missing history on an existing order must remain unknown, never repaired by seed.
            if created is not None:
                conn.execute(
                    insert(fulfillment_events)
                    .values(
                        tenant_id="demo",
                        id=f"seed-S0{index}",
                        order_id=order_id,
                        event_type="FULFILLMENT_FAILED",
                        error_code="QUEUE_PUBLISH_TIMEOUT",
                        created_at=now - timedelta(minutes=40),
                        operation_key=None,
                    )
                    .on_conflict_do_nothing()
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["demo", "approval-demo"], required=True)
    args = parser.parse_args()
    try:
        settings = get_simulator_settings()
    except ConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "message": str(exc)}))
        sys.exit(1)
    if settings.app_env not in {"local", "test"}:
        raise SystemExit("Seed is allowed only with APP_ENV=local or test.")
    engine = create_engine(settings.simulator_database_url, connect_args={"connect_timeout": 3})
    try:
        if args.scenario == "demo":
            seed_demo(engine, datetime.now(UTC))
        else:
            seed_approval_demo(engine, datetime.now(UTC))
    except SQLAlchemyError:
        print(json.dumps({"error": "SEED_DATABASE_ERROR"}))
        sys.exit(1)
    finally:
        engine.dispose()
    print(json.dumps({"status": "seeded", "scenarios": args.scenario, "reset": False}))


def seed_approval_demo(engine, now):
    """Additional fresh synthetic fixture; preserves the already-executed M1 sample."""
    with engine.begin() as conn:
        created = conn.execute(
            insert(orders)
            .values(
                tenant_id="demo",
                id="O3001",
                payment_status="PAID",
                fulfillment_status="FAILED",
                inventory_reserved=True,
                paid_at=now - timedelta(hours=1),
                version=3,
            )
            .on_conflict_do_nothing()
            .returning(orders.c.id)
        ).scalar_one_or_none()
        conn.execute(
            insert(tickets)
            .values(
                tenant_id="demo",
                id="T3001",
                subject="审批与恢复演示工单",
                status="OPEN",
                body="合成订单已付款一小时，履约任务未正常入队，请诊断。",
                bound_order_id="O3001",
            )
            .on_conflict_do_nothing()
        )
        if created is not None:
            conn.execute(
                insert(fulfillment_events)
                .values(
                    tenant_id="demo",
                    id="seed-approval",
                    order_id="O3001",
                    event_type="FULFILLMENT_FAILED",
                    error_code="QUEUE_PUBLISH_TIMEOUT",
                    created_at=now - timedelta(minutes=40),
                    operation_key=None,
                )
                .on_conflict_do_nothing()
            )


if __name__ == "__main__":
    main()
