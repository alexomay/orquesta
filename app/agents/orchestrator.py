"""Orquestador: coordina Triage -> Resolver (con herramientas) -> Revisor QA.

Decisiones de diseño:
- El ciclo de herramientas es propio (no el tool runner del SDK) para poder pausar en las
  aprobaciones humanas, emitir eventos en vivo y medir tokens y costo en cada llamada.
- El historial solo crece (append-only): nunca se editan mensajes previos, lo que mantiene
  válidos los bloques de thinking y aprovecha la caché de prompts.
- Cada evento se guarda en la base de datos y se publica en el bus para los WebSockets.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..events import EventBus
from ..llm import LLM, LLMError, LLMRequest, LLMResult, Usage
from ..models import Approval, Run, RunEvent, Ticket, utcnow
from ..pricing import cost_usd
from .prompts import (
    RESOLVER_SYSTEM,
    REVIEW_SCHEMA,
    REVIEWER_SYSTEM,
    TRIAGE_SCHEMA,
    TRIAGE_SYSTEM,
    render_ticket,
)
from .tools import REGISTRY, ToolContext, ToolError, tool_definitions

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000


class AgentError(RuntimeError):
    pass


@dataclass
class Decision:
    approved: bool
    reason: str | None = None
    decided_by: str | None = None


class ApprovalBroker:
    """Conecta la petición de aprobación (orquestador) con la decisión (API)."""

    def __init__(self) -> None:
        self._waiters: dict[int, asyncio.Future] = {}

    def register(self, approval_id: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters[approval_id] = fut
        return fut

    def is_waiting(self, approval_id: int) -> bool:
        return approval_id in self._waiters

    def resolve(self, approval_id: int, decision: Decision) -> bool:
        fut = self._waiters.pop(approval_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def discard(self, approval_id: int) -> None:
        self._waiters.pop(approval_id, None)


@dataclass
class RunState:
    run_id: int
    ticket_id: int
    seq: int = 0
    calls: int = 0
    usage: Usage = None  # type: ignore[assignment]
    cost: float = 0.0

    def __post_init__(self) -> None:
        self.usage = Usage()


class Orchestrator:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker,
        llm: LLM,
        bus: EventBus,
        broker: ApprovalBroker,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.llm = llm
        self.bus = bus
        self.broker = broker
        self.settings = settings
        self.tool_defs = tool_definitions()
        self._tasks: set[asyncio.Task] = set()

    # --- Ciclo de vida ------------------------------------------------------------------

    async def start_run(self, ticket_id: int) -> int:
        async with self.sessions() as s:
            run = Run(ticket_id=ticket_id, llm_mode=self.llm.name, model=self.llm.model)
            s.add(run)
            await s.commit()
            run_id = run.id
        task = asyncio.create_task(self._execute(run_id, ticket_id), name=f"run-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _execute(self, run_id: int, ticket_id: int) -> None:
        state = RunState(run_id, ticket_id)
        started = time.perf_counter()
        try:
            await self._update(Run, run_id, status="running", started_at=utcnow())
            async with self.sessions() as s:
                ticket = await s.get(Ticket, ticket_id)
                ticket_block = render_ticket(
                    ticket.id, ticket.subject, ticket.requester_email, ticket.body
                )
            await self._emit(
                state,
                "system",
                "run.started",
                {"ticket_id": ticket_id, "llm": self.llm.name, "model": self.llm.model},
            )

            triage = await self._triage(state, ticket_block)
            reply, review = await self._resolve_and_review(state, ticket_block, triage)

            async with self.sessions() as s:
                ticket = await s.get(Ticket, ticket_id)
                ticket.reply = reply
                ticket.status = "escalated" if ticket.escalated_to else "resolved"
                final_status = ticket.status
                await s.commit()
            duration = round(time.perf_counter() - started, 2)
            await self._finish(state, "completed")
            await self._emit(
                state,
                "system",
                "run.completed",
                {
                    "ticket_status": final_status,
                    "reply": reply,
                    "review": review,
                    "duration_s": duration,
                    **self._totals(state),
                },
            )
        except asyncio.CancelledError:
            await self._fail(state, "Ejecución interrumpida (el servidor se detuvo).")
            raise
        except (AgentError, LLMError) as e:
            await self._fail(state, str(e))
        except Exception as e:
            log.exception("Fallo inesperado en run %s", run_id)
            await self._fail(state, f"Error interno: {type(e).__name__}: {e}")

    async def _fail(self, state: RunState, message: str) -> None:
        await self._finish(state, "failed", error=message)
        await self._update(Ticket, state.ticket_id, status="failed")
        await self._emit(state, "system", "run.failed", {"error": message, **self._totals(state)})

    async def _finish(self, state: RunState, status: str, error: str | None = None) -> None:
        await self._update(
            Run,
            state.run_id,
            status=status,
            error=error,
            finished_at=utcnow(),
            **self._run_usage_fields(state),
        )

    # --- Agentes ------------------------------------------------------------------------

    async def _triage(self, state: RunState, ticket_block: str) -> dict[str, Any]:
        await self._emit(state, "triage", "agent.started", {})
        result = await self._call(
            state,
            LLMRequest(
                agent="triage",
                system=TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": ticket_block}],
                output_schema=TRIAGE_SCHEMA,
                effort="low",
                max_tokens=4000,
            ),
        )
        triage = self._parse_json(result, "triage")
        await self._update(
            Ticket,
            state.ticket_id,
            status="triaged",
            category=triage["category"],
            priority=triage["priority"],
            summary=triage["summary"],
            risk_flags=triage["risk_flags"],
        )
        await self._emit(state, "triage", "agent.finished", {"output": triage})
        return triage

    async def _resolve_and_review(
        self, state: RunState, ticket_block: str, triage: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        await self._update(Ticket, state.ticket_id, status="in_progress")
        await self._emit(state, "resolver", "agent.started", {})
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{ticket_block}\n\n<triage>{json.dumps(triage, ensure_ascii=False)}</triage>"
                    "\n\nResuelve el ticket."
                ),
            }
        ]
        reply = await self._resolver_loop(state, messages)

        review: dict[str, Any] = {}
        for round_ in range(self.settings.max_review_rounds + 1):
            await self._emit(state, "reviewer", "agent.started", {"round": round_ + 1})
            result = await self._call(
                state,
                LLMRequest(
                    agent="reviewer",
                    system=REVIEWER_SYSTEM,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"{ticket_block}\n\n<respuesta_propuesta>\n{reply}\n</respuesta_propuesta>"
                            ),
                        }
                    ],
                    output_schema=REVIEW_SCHEMA,
                    effort="low",
                    max_tokens=4000,
                ),
            )
            review = self._parse_json(result, "reviewer")
            await self._emit(state, "reviewer", "agent.finished", {"output": review})
            if review["approved"] or round_ == self.settings.max_review_rounds:
                break
            # Se agrega un mensaje nuevo en lugar de editar el historial.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "El revisor de calidad pidió cambios antes de enviar la respuesta:\n- "
                        + "\n- ".join(review["issues"])
                        + f"\nSugerencia: {review['suggested_fix']}"
                        + "\nEscribe la respuesta final corregida."
                    ),
                }
            )
            await self._emit(state, "resolver", "agent.started", {"revision": round_ + 1})
            reply = await self._resolver_loop(state, messages)
        return reply, review

    async def _resolver_loop(self, state: RunState, messages: list[dict[str, Any]]) -> str:
        for _ in range(self.settings.max_agent_turns):
            result = await self._call(
                state,
                LLMRequest(
                    agent="resolver",
                    system=RESOLVER_SYSTEM,
                    messages=messages,
                    tools=self.tool_defs,
                    effort="medium",
                ),
            )
            messages.append({"role": "assistant", "content": result.content})
            if result.stop_reason == "pause_turn":
                continue

            tool_uses = result.tool_uses()
            if not tool_uses:
                reply = result.text()
                if not reply:
                    raise AgentError("El agente resolutor terminó sin respuesta.")
                await self._emit(state, "resolver", "agent.finished", {"reply": reply})
                return reply

            if text := result.text():
                await self._emit(state, "resolver", "agent.message", {"text": text})
            # Herramientas independientes en paralelo; todos los resultados van en un solo
            # mensaje de usuario, en el mismo orden.
            results = await asyncio.gather(*(self._run_tool(state, tu) for tu in tool_uses))
            messages.append({"role": "user", "content": list(results)})
        raise AgentError(
            f"El agente resolutor superó el límite de {self.settings.max_agent_turns} turnos."
        )

    # --- Herramientas y aprobaciones ----------------------------------------------------

    async def _run_tool(self, state: RunState, tu: dict[str, Any]) -> dict[str, Any]:
        tool = REGISTRY.get(tu["name"])
        await self._emit(
            state,
            "resolver",
            "tool.called",
            {
                "tool_use_id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
                "requires_approval": bool(tool and tool.requires_approval),
            },
        )

        def tool_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
            content = json.dumps(payload, ensure_ascii=False)[:MAX_RESULT_CHARS]
            return {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": is_error,
            }

        started = time.perf_counter()
        if tool is None:
            out, is_error = {"error": f"La herramienta {tu['name']} no existe."}, True
        else:
            try:
                args = tool.input_model.model_validate(tu["input"])
            except ValidationError as e:
                out, is_error = {"error": f"Entrada inválida: {e.errors()}"}, True
            else:
                decision = Decision(approved=True)
                if tool.requires_approval:
                    decision = await self._request_approval(state, tool.name, tu["input"])
                if not decision.approved:
                    out, is_error = (
                        {
                            "error": "Acción denegada por el operador humano.",
                            "reason": decision.reason or "Sin motivo",
                        },
                        True,
                    )
                else:
                    try:
                        ctx = ToolContext(self.sessions, state.ticket_id, state.run_id)
                        out, is_error = await tool.func(ctx, args), False
                    except ToolError as e:
                        out, is_error = {"error": str(e)}, True

        await self._emit(
            state,
            "resolver",
            "tool.result",
            {
                "tool_use_id": tu["id"],
                "name": tu["name"],
                "output": out,
                "is_error": is_error,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return tool_result(out, is_error)

    async def _request_approval(
        self, state: RunState, tool_name: str, tool_input: dict[str, Any]
    ) -> Decision:
        async with self.sessions() as s:
            approval = Approval(
                run_id=state.run_id,
                ticket_id=state.ticket_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
            s.add(approval)
            await s.commit()
            approval_id = approval.id

        fut = self.broker.register(approval_id)  # antes de avisar, para no perder la respuesta
        await self._update(Run, state.run_id, status="awaiting_approval")
        await self._update(Ticket, state.ticket_id, status="awaiting_approval")
        await self._emit(
            state,
            "resolver",
            "approval.requested",
            {
                "approval_id": approval_id,
                "tool": tool_name,
                "input": tool_input,
            },
        )
        try:
            decision: Decision = await asyncio.wait_for(fut, self.settings.approval_timeout_s)
        except TimeoutError:
            self.broker.discard(approval_id)
            decision = Decision(approved=False, reason="La aprobación expiró.")
            await self._update(
                Approval, approval_id, status="expired", decided_at=utcnow(), reason=decision.reason
            )

        await self._update(Run, state.run_id, status="running")
        await self._update(Ticket, state.ticket_id, status="in_progress")
        await self._emit(
            state,
            "resolver",
            "approval.decided",
            {
                "approval_id": approval_id,
                "approved": decision.approved,
                "reason": decision.reason,
                "decided_by": decision.decided_by,
            },
        )
        return decision

    # --- LLM, costos y eventos ----------------------------------------------------------

    async def _call(self, state: RunState, req: LLMRequest) -> LLMResult:
        result = await self.llm.complete(req)
        cost = cost_usd(result.model, result.usage)
        state.calls += 1
        state.cost += cost
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            setattr(state.usage, field, getattr(state.usage, field) + getattr(result.usage, field))

        for summary in result.thinking_summaries():
            await self._emit(state, req.agent, "agent.thinking", {"text": summary})
        await self._emit(
            state,
            req.agent,
            "llm.usage",
            {
                "model": result.model,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cache_read_tokens": result.usage.cache_read_tokens,
                "cache_write_tokens": result.usage.cache_write_tokens,
                "cost_usd": cost,
                "latency_ms": result.latency_ms,
                "totals": self._totals(state),
            },
        )
        await self._update(Run, state.run_id, **self._run_usage_fields(state))

        if result.stop_reason == "refusal":
            category = (result.stop_details or {}).get("category")
            raise AgentError(f"El modelo rechazó la solicitud (categoría: {category}).")
        if result.stop_reason == "max_tokens":
            raise AgentError(f"El agente {req.agent} se quedó sin tokens de salida.")
        return result

    def _parse_json(self, result: LLMResult, agent: str) -> dict[str, Any]:
        try:
            return json.loads(result.text())
        except json.JSONDecodeError as e:
            raise AgentError(f"El agente {agent} devolvió JSON inválido.") from e

    @staticmethod
    def _totals(state: RunState) -> dict[str, Any]:
        return {
            "llm_calls": state.calls,
            "input_tokens": state.usage.input_tokens,
            "output_tokens": state.usage.output_tokens,
            "cache_read_tokens": state.usage.cache_read_tokens,
            "cost_usd": round(state.cost, 6),
        }

    @staticmethod
    def _run_usage_fields(state: RunState) -> dict[str, Any]:
        return {
            "llm_calls": state.calls,
            "input_tokens": state.usage.input_tokens,
            "output_tokens": state.usage.output_tokens,
            "cache_read_tokens": state.usage.cache_read_tokens,
            "cache_write_tokens": state.usage.cache_write_tokens,
            "cost_usd": round(state.cost, 6),
        }

    async def _update(self, model: type, row_id: int, **values: Any) -> None:
        async with self.sessions() as s:
            await s.execute(update(model).where(model.id == row_id).values(**values))
            await s.commit()

    async def _emit(self, state: RunState, agent: str, type_: str, payload: dict) -> None:
        state.seq += 1
        async with self.sessions() as s:
            ev = RunEvent(
                run_id=state.run_id, seq=state.seq, agent=agent, type=type_, payload=payload
            )
            s.add(ev)
            await s.commit()
            created = ev.created_at
        self.bus.publish(
            {
                "run_id": state.run_id,
                "ticket_id": state.ticket_id,
                "seq": state.seq,
                "agent": agent,
                "type": type_,
                "payload": payload,
                "created_at": created.isoformat(),
            }
        )
