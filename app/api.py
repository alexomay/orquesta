import asyncio
import secrets
from collections import Counter
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select

from . import __version__
from .agents.orchestrator import Decision
from .agents.tools import tool_definitions
from .models import Approval, Run, RunEvent, Ticket, TicketNote, utcnow
from .schemas import (
    ApprovalDecision,
    ApprovalOut,
    EventOut,
    NoteOut,
    RunDetail,
    RunOut,
    TicketCreate,
    TicketCreated,
    TicketDetail,
    TicketOut,
)
from .seed import SAMPLE_TICKETS

router = APIRouter(prefix="/api")
ws_router = APIRouter()


def state(request: Request):
    return request.app.state


State = Annotated[object, Depends(state)]


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.admin_token
    if expected and not (x_admin_token and secrets.compare_digest(x_admin_token, expected)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token de administrador inválido")


@router.get("/health")
async def health(st: State):
    return {
        "status": "ok",
        "version": __version__,
        "llm": st.orchestrator.llm.name,
        "model": st.orchestrator.llm.model,
        "approvals_protected": bool(st.settings.admin_token),
    }


@router.get("/samples")
async def samples():
    return SAMPLE_TICKETS


@router.get("/tools")
async def tools():
    from .agents.tools import REGISTRY

    return [
        {**d, "requires_approval": REGISTRY[d["name"]].requires_approval}
        for d in tool_definitions()
    ]


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(st: State, limit: int = 50):
    async with st.sessions() as s:
        rows = await s.scalars(select(Ticket).order_by(Ticket.id.desc()).limit(min(limit, 200)))
        return rows.all()


@router.post("/tickets", response_model=TicketCreated, status_code=201)
async def create_ticket(data: TicketCreate, st: State):
    async with st.sessions() as s:
        ticket = Ticket(
            subject=data.subject.strip(),
            body=data.body.strip(),
            requester_email=data.requester_email.strip().lower(),
            risk_flags=[],
        )
        s.add(ticket)
        await s.commit()
    run_id = await st.orchestrator.start_run(ticket.id)
    return TicketCreated(ticket=TicketOut.model_validate(ticket), run_id=run_id)


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(ticket_id: int, st: State):
    async with st.sessions() as s:
        ticket = await s.get(Ticket, ticket_id)
        if not ticket:
            raise HTTPException(404, "Ticket no encontrado")
        runs = (
            await s.scalars(select(Run).where(Run.ticket_id == ticket_id).order_by(Run.id.desc()))
        ).all()
        notes = (
            await s.scalars(
                select(TicketNote).where(TicketNote.ticket_id == ticket_id).order_by(TicketNote.id)
            )
        ).all()
        approvals = (
            await s.scalars(
                select(Approval).where(Approval.ticket_id == ticket_id).order_by(Approval.id)
            )
        ).all()
    return TicketDetail(
        ticket=TicketOut.model_validate(ticket),
        runs=[RunOut.model_validate(r) for r in runs],
        notes=[NoteOut.model_validate(n) for n in notes],
        approvals=[ApprovalOut.model_validate(a) for a in approvals],
    )


@router.post("/tickets/{ticket_id}/runs", status_code=201)
async def rerun_ticket(ticket_id: int, st: State):
    async with st.sessions() as s:
        ticket = await s.get(Ticket, ticket_id)
        if not ticket:
            raise HTTPException(404, "Ticket no encontrado")
        active = await s.scalar(
            select(Run.id).where(
                Run.ticket_id == ticket_id,
                Run.status.in_(["queued", "running", "awaiting_approval"]),
            )
        )
        if active:
            raise HTTPException(409, "El ticket ya tiene una ejecución activa")
        ticket.escalated_to = None
        await s.commit()
    return {"run_id": await st.orchestrator.start_run(ticket_id)}


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, st: State):
    async with st.sessions() as s:
        run = await s.get(Run, run_id)
        if not run:
            raise HTTPException(404, "Ejecución no encontrada")
        events = (
            await s.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
        ).all()
    return RunDetail(
        run=RunOut.model_validate(run), events=[EventOut.model_validate(e) for e in events]
    )


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(st: State, status_: str | None = Query(default=None, alias="status")):
    async with st.sessions() as s:
        q = select(Approval).order_by(Approval.id.desc()).limit(100)
        if status_:
            q = q.where(Approval.status == status_)
        return (await s.scalars(q)).all()


@router.post(
    "/approvals/{approval_id}", response_model=ApprovalOut, dependencies=[Depends(require_admin)]
)
async def decide_approval(approval_id: int, data: ApprovalDecision, st: State):
    async with st.sessions() as s:
        approval = await s.get(Approval, approval_id)
        if not approval:
            raise HTTPException(404, "Aprobación no encontrada")
        if approval.status != "pending":
            raise HTTPException(409, f"La aprobación ya está en estado '{approval.status}'")
        if not st.broker.is_waiting(approval_id):
            approval.status = "expired"
            approval.decided_at = utcnow()
            await s.commit()
            raise HTTPException(409, "La ejecución ya no está activa; la aprobación expiró")
        approved = data.decision == "approve"
        approval.status = "approved" if approved else "denied"
        approval.reason = data.reason
        approval.decided_by = data.decided_by
        approval.decided_at = utcnow()
        await s.commit()
    st.broker.resolve(approval_id, Decision(approved, data.reason, data.decided_by))
    return ApprovalOut.model_validate(approval)


@router.get("/metrics")
async def metrics(st: State):
    async with st.sessions() as s:
        tickets = (await s.scalars(select(Ticket))).all()
        runs = (await s.scalars(select(Run))).all()
        approvals = (await s.scalars(select(Approval))).all()
    done = [r for r in runs if r.status == "completed"]
    durations = [
        (r.finished_at - r.started_at).total_seconds()
        for r in done
        if r.finished_at and r.started_at
    ]
    resolved = sum(t.status == "resolved" for t in tickets)
    finished = sum(t.status in ("resolved", "escalated") for t in tickets)
    return {
        "tickets": len(tickets),
        "tickets_by_status": dict(Counter(t.status for t in tickets)),
        "tickets_by_category": dict(Counter(t.category or "sin_clasificar" for t in tickets)),
        "auto_resolution_rate": round(resolved / finished, 3) if finished else None,
        "runs": len(runs),
        "runs_failed": sum(r.status == "failed" for r in runs),
        "total_cost_usd": round(sum(r.cost_usd for r in runs), 4),
        "avg_cost_usd": round(sum(r.cost_usd for r in done) / len(done), 4) if done else None,
        "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "total_tokens": sum(r.input_tokens + r.output_tokens for r in runs),
        "cache_read_tokens": sum(r.cache_read_tokens for r in runs),
        "approvals": dict(Counter(a.status for a in approvals)),
    }


@ws_router.websocket("/ws/events")
async def events_ws(ws: WebSocket):
    await ws.accept()
    bus = ws.app.state.bus
    queue = bus.subscribe()
    try:
        await ws.send_json({"type": "hello", "llm": ws.app.state.orchestrator.llm.name})
        while True:
            # Ping cada 25 s para que proxies no cierren la conexión inactiva.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
            except TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
