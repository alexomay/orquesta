import httpx

from app.config import Settings
from app.llm.fake_llm import FakeLLM
from app.main import create_app

from .conftest import wait_for


async def test_health_reports_demo_mode(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["llm"] == "fake"


async def test_ticket_validation(client):
    r = await client.post(
        "/api/tickets", json={"subject": "x", "body": "hola", "requester_email": "no-es-correo"}
    )
    assert r.status_code == 422


async def test_unknown_resources_return_404(client):
    assert (await client.get("/api/tickets/999")).status_code == 404
    assert (await client.get("/api/runs/999")).status_code == 404
    r = await client.post("/api/approvals/999", json={"decision": "approve"})
    assert r.status_code == 404


async def test_metrics_after_runs(client, app):
    await client.post(
        "/api/tickets",
        json={
            "subject": "VPN",
            "body": "La VPN no conecta desde casa",
            "requester_email": "sofia.mendez@nexa.mx",
        },
    )
    await app.state.orchestrator.wait_idle()
    m = (await client.get("/api/metrics")).json()
    assert m["tickets"] == 1 and m["runs"] == 1
    assert m["auto_resolution_rate"] == 1.0
    assert m["total_cost_usd"] > 0


async def test_approvals_require_admin_token_when_configured(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}",
        llm_mode="fake",
        admin_token="s3creto",
        _env_file=None,
    )
    app = create_app(settings, llm=FakeLLM(latency_s=0))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.post(
                "/api/tickets",
                json={
                    "subject": "Cuenta bloqueada",
                    "body": "Mi cuenta se bloqueó",
                    "requester_email": "ana.torres@nexa.mx",
                },
            )

            async def pending():
                rows = (await client.get("/api/approvals", params={"status": "pending"})).json()
                return rows[0] if rows else None

            approval = await wait_for(pending)
            url = f"/api/approvals/{approval['id']}"
            assert (await client.post(url, json={"decision": "approve"})).status_code == 401
            r = await client.post(
                url, json={"decision": "approve"}, headers={"X-Admin-Token": "s3creto"}
            )
            assert r.status_code == 200
            await app.state.orchestrator.wait_idle()


async def test_decision_twice_is_rejected(client, app):
    await client.post(
        "/api/tickets",
        json={
            "subject": "Cuenta bloqueada",
            "body": "Mi cuenta se bloqueó",
            "requester_email": "ana.torres@nexa.mx",
        },
    )

    async def pending():
        rows = (await client.get("/api/approvals", params={"status": "pending"})).json()
        return rows[0] if rows else None

    approval = await wait_for(pending)
    url = f"/api/approvals/{approval['id']}"
    assert (await client.post(url, json={"decision": "approve"})).status_code == 200
    assert (await client.post(url, json={"decision": "deny"})).status_code == 409
    await app.state.orchestrator.wait_idle()
