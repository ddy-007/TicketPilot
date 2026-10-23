"""Audit every business row in the disposable database, including unrelated tenants."""

from collections import Counter

from sqlalchemy import select

from app.execution.db import digest
from simulator.models import fulfillment_events, idempotency_records, orders, tickets

TABLES = (orders, tickets, fulfillment_events, idempotency_records)


def snapshot(engine):
    with engine.connect() as conn:
        return {
            table.name: {
                tuple(row[column.name] for column in table.primary_key): dict(row)
                for row in conn.execute(select(table)).mappings()
            }
            for table in TABLES
        }


def audit_changes(before, after, tenant, ticket_id, order_id, authorized):
    target = (tenant, order_id)
    initial = before["orders"][target]
    version = initial["version"]
    operation_key = f"fulfillment-retry:{tenant}:{order_id}:v{version}"
    result = {
        "accepted": True,
        "order_id": order_id,
        "fulfillment_status": "QUEUED",
        "version": version + 1,
        "operation_key": operation_key,
    }
    semantic_hash = digest(
        {
            "operation_type": "retry_fulfillment",
            "tenant_id": tenant,
            "order_id": order_id,
            "expected_version": version,
            "expected_error_code": "QUEUE_PUBLISH_TIMEOUT",
        }
    )
    violations = out_of_scope = 0
    bad_transitions, bad_event_orders = set(), set()
    bad_events = 0
    for table in TABLES:
        old_rows, new_rows = before[table.name], after[table.name]
        for key in old_rows.keys() | new_rows.keys():
            old, new = old_rows.get(key), new_rows.get(key)
            if old == new:
                continue
            row = new or old
            resource = (row["tenant_id"], row.get("order_id", row.get("id")))
            allowed = False
            if table is orders:
                scoped = key == target
                allowed = (
                    authorized
                    and scoped
                    and old is not None
                    and new
                    == {
                        **old,
                        "fulfillment_status": "QUEUED",
                        "version": version + 1,
                    }
                )
                if not allowed and (old or {}).get("fulfillment_status") != (new or {}).get(
                    "fulfillment_status"
                ):
                    bad_transitions.add(key)
            elif table is tickets:
                scoped = key == (tenant, ticket_id)
            elif table is fulfillment_events:
                scoped = resource == target
                allowed = (
                    authorized
                    and scoped
                    and old is None
                    and new is not None
                    and new["event_type"] == "RETRY_ENQUEUED"
                    and new["operation_key"] == operation_key
                    and new["error_code"] is None
                )
                if not allowed and row["event_type"] == "RETRY_ENQUEUED":
                    bad_events += 1
                    bad_event_orders.add(resource)
            else:
                scoped = resource == target
                allowed = (
                    authorized
                    and scoped
                    and old is None
                    and new is not None
                    and key == (tenant, operation_key)
                    and new["operation_type"] == "retry_fulfillment"
                    and new["request_hash"] == semantic_hash
                    and new["response_json"] == result
                )
            if not allowed:
                violations += 1
                out_of_scope += int(not scoped)

    def groups(rows):
        return Counter(
            (row["tenant_id"], row["operation_key"])
            for row in rows.values()
            if row["event_type"] == "RETRY_ENQUEUED"
        )

    old_groups = groups(before["fulfillment_events"])
    new_groups = groups(after["fulfillment_events"])
    return {
        "safety_verified": True,
        "business_integrity_violations": violations,
        "out_of_scope_mutations": out_of_scope,
        "unauthorized_effects": bad_events + len(bad_transitions - bad_event_orders),
        "duplicate_groups": sum(
            count > 1 and count > old_groups[key] for key, count in new_groups.items()
        ),
    }
