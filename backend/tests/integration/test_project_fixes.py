"""Regression coverage for the whole-project audit findings."""

import httpx
import pytest
from sqlalchemy import select

from app.execution.business import BusinessClient, RemoteError
from simulator.models import tickets
from tests.integration.test_m3_execution import (  # noqa: F401
    OPERATOR,
    READ,
    case,
    infrastructure,
)


@pytest.mark.parametrize("uncertain", [False, True])
def test_rejected_run_keeps_its_decision_when_another_run_shares_operation(case, uncertain):  # noqa: F811
    c, api = case
    first, approval = c.waiting(api)
    with c.engine.begin() as conn:
        conn.execute(
            tickets.insert().values(
                tenant_id="demo",
                id="T_SECOND",
                subject="Same order",
                body="Synthetic second ticket",
                bound_order_id="O2001",
                status="OPEN",
            )
        )
    second = c.start(api, ticket="T_SECOND")
    c.tick()
    second_approval = c.db.approval(c.db.run(second)["approval_id"])
    assert c.decision(api, approval).status_code == 202

    class LostResponse(BusinessClient):
        def request(self, method, path, *args, **kwargs):
            if method == "GET" and path.startswith("/internal/operations/"):
                raise RemoteError("BUSINESS_UNAVAILABLE")
            result = super().request(method, path, *args, **kwargs)
            if method == "POST":
                raise RemoteError("BUSINESS_UNAVAILABLE")
            return result

    c.tick(**({"business_factory": lambda: LostResponse(c.settings)} if uncertain else {}))
    assert len(c.effects()) == 1
    assert c.decision(api, second_approval, "REJECTED").status_code == 202
    c.tick()
    run = c.db.run(second)
    result = run["result_json"]
    assert (run["status"], run["disposition"]) == ("REJECTED", "DECLINED")
    assert result["executed"] is False and result["operation"] is None
    assert result["historical_operation"]["origin_run_id"] == str(first)
    assert result["historical_operation"]["status"] == ("UNKNOWN" if uncertain else "CONFIRMED")
    assert result["warnings"] and len(c.effects()) == 1
    with c.engine.connect() as conn:
        assert conn.execute(select(c.db.operations.c.post_attempts)).scalar_one() == 1


def test_ticket_pages_bound_bytes_preserve_details_and_filter_tenants(case):  # noqa: F811
    c, api = case
    with c.engine.begin() as conn:
        conn.execute(
            tickets.insert(),
            [
                {
                    "tenant_id": "demo",
                    "id": f"T_LONG_{i:03}",
                    "subject": "题" * 200,
                    "body": "文" * 2000,
                    "bound_order_id": None,
                    "status": "OPEN",
                }
                for i in range(70)
            ],
        )
        conn.execute(
            tickets.insert().values(
                tenant_id="other",
                id="T_PRIVATE",
                subject="Other tenant",
                body="Synthetic",
                bound_order_id=None,
                status="OPEN",
            )
        )
    auth = {"Authorization": "Bearer " + OPERATOR}
    seen, cursor = [], ""
    with httpx.Client(trust_env=False) as remote:
        while True:
            params = {"after_id": cursor, "limit": 13}
            upstream = remote.get(
                c.settings.simulator_base_url + "/internal/tickets",
                params=params,
                headers={"Authorization": "Bearer " + READ, "X-Tenant-ID": "demo"},
            )
            assert upstream.status_code == 200 and len(upstream.content) <= 65536
            response = api.get("/api/tickets", params=params, headers=auth)
            assert response.status_code == 200
            page = response.json()
            assert 0 < len(page["tickets"]) <= 13
            assert all("body" not in row and row["tenant_id"] == "demo" for row in page["tickets"])
            seen.extend(row["id"] for row in page["tickets"])
            if page["next_after_id"] is None:
                break
            assert page["next_after_id"] > cursor
            cursor = page["next_after_id"]
    assert len(seen) == len(set(seen)) == 76
    assert "T_PRIVATE" not in seen
    detail = api.get("/api/tickets/T_LONG_069", headers=auth)
    assert detail.status_code == 200 and detail.json()["body"] == "文" * 2000
    for params in ({"limit": 26}, {"limit": 0}, {"after_id": "bad/cursor"}):
        assert api.get("/api/tickets", params=params, headers=auth).status_code == 422
