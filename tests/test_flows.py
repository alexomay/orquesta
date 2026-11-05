"""Flujos completos de punta a punta con el LLM simulado."""

from app.seed import SAMPLE_TICKETS

from .conftest import wait_for

SAMPLES = {s["key"]: s for s in SAMPLE_TICKETS}


async def create(client, key: str) -> tuple[int, int]:
    s = SAMPLES[key]
    r = await client.post(
        "/api/tickets",
        json={"subject": s["subject"], "body": s["body"], "requester_email": s["requester_email"]},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["ticket"]["id"], data["run_id"]


async def run_status(client, run_id: int) -> dict:
    return (await client.get(f"/api/runs/{run_id}")).json()


def event_types(detail: dict) -> list[str]:
    return [e["type"] for e in detail["events"]]


async def test_vpn_ticket_is_resolved_with_kb_and_service_status(client, app):
    ticket_id, run_id = await create(client, "vpn")
    await app.state.orchestrator.wait_idle()

    detail = await run_status(client, run_id)
    assert detail["run"]["status"] == "completed", detail["run"]["error"]
    types = event_types(detail)
    assert types[0] == "run.started" and types[-1] == "run.completed"
    called = [e["payload"]["name"] for e in detail["events"] if e["type"] == "tool.called"]
    assert {"search_knowledge_base", "get_employee", "check_service_status"} <= set(called)
    assert "approval.requested" not in types
    assert detail["run"]["cost_usd"] > 0 and detail["run"]["llm_calls"] == 4

    ticket = (await client.get(f"/api/tickets/{ticket_id}")).json()["ticket"]
    assert ticket["status"] == "resolved"
    assert ticket["category"] == "red"
    assert "VPN" in ticket["reply"] and "incidente" in ticket["reply"]


async def test_password_reset_waits_for_human_approval(client, app):
    ticket_id, run_id = await create(client, "password")

    async def pending():
        rows = (await client.get("/api/approvals", params={"status": "pending"})).json()
        return rows[0] if rows else None

    approval = await wait_for(pending)
    assert approval["tool_name"] == "reset_password"
    assert approval["tool_input"]["email"] == "ana.torres@nexa.mx"
    assert (await run_status(client, run_id))["run"]["status"] == "awaiting_approval"

    r = await client.post(
        f"/api/approvals/{approval['id']}", json={"decision": "approve", "decided_by": "pytest"}
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    await app.state.orchestrator.wait_idle()

    ticket = (await client.get(f"/api/tickets/{ticket_id}")).json()
    assert ticket["ticket"]["status"] == "resolved"
    assert "desbloqueamos" in ticket["ticket"]["reply"]
    assert ticket["approvals"][0]["decided_by"] == "pytest"


async def test_denied_reset_is_escalated(client, app):
    ticket_id, _ = await create(client, "password")

    async def pending():
        rows = (await client.get("/api/approvals", params={"status": "pending"})).json()
        return rows[0] if rows else None

    approval = await wait_for(pending)
    await client.post(
        f"/api/approvals/{approval['id']}",
        json={"decision": "deny", "reason": "Validar identidad por teléfono"},
    )
    await app.state.orchestrator.wait_idle()

    ticket = (await client.get(f"/api/tickets/{ticket_id}")).json()["ticket"]
    assert ticket["status"] == "escalated"
    assert ticket["escalated_to"] == "Mesa de ayuda N2"


async def test_social_engineering_is_blocked_without_touching_accounts(client, app):
    ticket_id, run_id = await create(client, "injection")
    await app.state.orchestrator.wait_idle()

    detail = await run_status(client, run_id)
    called = [e["payload"]["name"] for e in detail["events"] if e["type"] == "tool.called"]
    assert "reset_password" not in called
    assert "approval.requested" not in event_types(detail)

    data = (await client.get(f"/api/tickets/{ticket_id}")).json()
    assert data["ticket"]["status"] == "escalated"
    assert data["ticket"]["escalated_to"] == "Seguridad"
    assert "ingenieria_social" in data["ticket"]["risk_flags"]
    assert data["ticket"]["priority"] == "critica"
    assert any("ingeniería social" in n["text"] for n in data["notes"])


async def test_hardware_goes_to_onsite_support(client, app):
    ticket_id, _ = await create(client, "hardware")
    await app.state.orchestrator.wait_idle()
    ticket = (await client.get(f"/api/tickets/{ticket_id}")).json()["ticket"]
    assert ticket["status"] == "escalated"
    assert ticket["escalated_to"] == "Soporte en sitio"


async def test_events_are_streamed_to_subscribers(client, app):
    queue = app.state.bus.subscribe()
    _, run_id = await create(client, "vpn")
    await app.state.orchestrator.wait_idle()
    received = []
    while not queue.empty():
        received.append(queue.get_nowait())
    assert received and all(e["run_id"] == run_id for e in received)
    assert [e["seq"] for e in received] == sorted(e["seq"] for e in received)
    assert received[-1]["type"] == "run.completed"
