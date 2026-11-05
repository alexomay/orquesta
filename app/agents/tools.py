"""Herramientas que pueden usar los agentes.

Cada herramienta declara su entrada con un modelo Pydantic (de ahí sale el JSON Schema estricto
que recibe Claude) y si requiere aprobación humana antes de ejecutarse.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import Asset, Employee, Service, Ticket, TicketNote
from ..rag import Retriever
from ..text import norm


class ToolError(Exception):
    """Error esperado (dato no encontrado, entrada inválida); se le devuelve al agente."""


@dataclass
class ToolContext:
    sessions: async_sessionmaker
    ticket_id: int
    run_id: int
    retriever: Retriever


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


ToolFunc = Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    func: ToolFunc
    requires_approval: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _clean_schema(self.input_model.model_json_schema()),
            "strict": True,
        }


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Quita los 'title' que agrega Pydantic; el resto del esquema queda igual."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, input_model: type[ToolInput], *, requires_approval=False):
    def decorator(func: ToolFunc) -> ToolFunc:
        REGISTRY[name] = Tool(name, description, input_model, func, requires_approval)
        return func

    return decorator


def tool_definitions() -> list[dict[str, Any]]:
    # Orden estable: cambiar el orden invalidaría la caché de prompts.
    return [REGISTRY[name].definition() for name in sorted(REGISTRY)]


async def _employee(ctx: ToolContext, email: str) -> Employee:
    async with ctx.sessions() as s:
        emp = await s.scalar(select(Employee).where(Employee.email == email.strip().lower()))
    if not emp:
        raise ToolError(f"No existe un empleado con el correo {email}")
    return emp


# --- Herramientas de solo lectura -----------------------------------------------------


class SearchKB(ToolInput):
    query: str = Field(description="El problema descrito con tus palabras, en español.")


@tool(
    "search_knowledge_base",
    "Busca artículos en la base de conocimiento de TI. Devuelve hasta 3 artículos con sus "
    "pasos de solución.",
    SearchKB,
)
async def search_knowledge_base(ctx: ToolContext, args: SearchKB) -> dict[str, Any]:
    hits = await ctx.retriever.search(args.query, k=3)
    return {
        "query": args.query,
        "method": ctx.retriever.mode,
        "results": [
            {"id": h.id, "title": h.title, "content": h.content, "score": h.score, "ranks": h.ranks}
            for h in hits
        ],
    }


class EmailInput(ToolInput):
    email: str = Field(description="Correo corporativo del empleado.")


@tool(
    "get_employee",
    "Obtiene el perfil de un empleado: nombre, área, puesto, si es VIP y el estado de su cuenta.",
    EmailInput,
)
async def get_employee(ctx: ToolContext, args: EmailInput) -> dict[str, Any]:
    e = await _employee(ctx, args.email)
    return {
        "email": e.email,
        "name": e.name,
        "department": e.department,
        "title": e.title,
        "is_vip": e.is_vip,
        "account_locked": e.account_locked,
        "password_reset_pending": e.password_reset_pending,
    }


@tool(
    "list_employee_assets",
    "Lista los equipos asignados a un empleado (laptop, monitor, celular).",
    EmailInput,
)
async def list_employee_assets(ctx: ToolContext, args: EmailInput) -> dict[str, Any]:
    e = await _employee(ctx, args.email)
    async with ctx.sessions() as s:
        assets = (await s.scalars(select(Asset).where(Asset.employee_id == e.id))).all()
    return {
        "email": e.email,
        "assets": [
            {"tag": a.tag, "kind": a.kind, "model": a.model, "status": a.status} for a in assets
        ],
    }


class ServiceInput(ToolInput):
    service: str = Field(description="Nombre del servicio, por ejemplo VPN, Correo, ERP, WiFi.")


@tool(
    "check_service_status",
    "Consulta el estado actual de un servicio de TI (operativo, degradado o caído) y si hay "
    "un incidente abierto.",
    ServiceInput,
)
async def check_service_status(ctx: ToolContext, args: ServiceInput) -> dict[str, Any]:
    async with ctx.sessions() as s:
        services = (await s.scalars(select(Service))).all()
    wanted = norm(args.service)
    for svc in services:
        if norm(svc.name) == wanted:
            return {"name": svc.name, "status": svc.status, "note": svc.note}
    raise ToolError(
        f"Servicio '{args.service}' no encontrado. Disponibles: "
        + ", ".join(s.name for s in services)
    )


# --- Herramientas que escriben (riesgo bajo) ------------------------------------------


class NoteInput(ToolInput):
    note: str = Field(description="Nota interna para el equipo de TI; el solicitante no la ve.")


@tool("add_internal_note", "Agrega una nota interna al ticket.", NoteInput)
async def add_internal_note(ctx: ToolContext, args: NoteInput) -> dict[str, Any]:
    async with ctx.sessions() as s:
        s.add(TicketNote(ticket_id=ctx.ticket_id, author="agente:resolver", text=args.note))
        await s.commit()
    return {"saved": True}


class EscalateInput(ToolInput):
    team: str = Field(
        description="Equipo destino: Mesa de ayuda N2, Soporte en sitio, Redes, Seguridad o ERP."
    )
    reason: str = Field(description="Por qué se escala, en una oración.")


@tool(
    "escalate_to_human",
    "Turna el ticket a un equipo humano cuando no se puede resolver de forma automática o "
    "cuando hay un riesgo de seguridad.",
    EscalateInput,
)
async def escalate_to_human(ctx: ToolContext, args: EscalateInput) -> dict[str, Any]:
    async with ctx.sessions() as s:
        ticket = await s.get(Ticket, ctx.ticket_id)
        ticket.escalated_to = args.team
        s.add(
            TicketNote(
                ticket_id=ctx.ticket_id,
                author="agente:resolver",
                text=f"Escalado a {args.team}: {args.reason}",
            )
        )
        await s.commit()
    return {"escalated_to": args.team}


# --- Acciones sensibles: requieren aprobación humana ----------------------------------


class ResetInput(ToolInput):
    email: str = Field(description="Correo del titular de la cuenta.")
    reason: str = Field(description="Justificación que verá el operador que aprueba.")


@tool(
    "reset_password",
    "Desbloquea la cuenta y envía al celular registrado del titular un enlace para crear una "
    "contraseña nueva. Requiere aprobación de un operador humano. Nunca devuelve contraseñas.",
    ResetInput,
    requires_approval=True,
)
async def reset_password(ctx: ToolContext, args: ResetInput) -> dict[str, Any]:
    async with ctx.sessions() as s:
        emp = await s.scalar(select(Employee).where(Employee.email == args.email.strip().lower()))
        if not emp:
            raise ToolError(f"No existe un empleado con el correo {args.email}")
        emp.account_locked = False
        emp.password_reset_pending = True
        await s.commit()
    return {"done": True, "unlocked": True, "reset_link_sent_to": "celular registrado"}


@tool(
    "unlock_account",
    "Desbloquea la cuenta sin cambiar la contraseña. Requiere aprobación de un operador humano.",
    EmailInput,
    requires_approval=True,
)
async def unlock_account(ctx: ToolContext, args: EmailInput) -> dict[str, Any]:
    async with ctx.sessions() as s:
        emp = await s.scalar(select(Employee).where(Employee.email == args.email.strip().lower()))
        if not emp:
            raise ToolError(f"No existe un empleado con el correo {args.email}")
        emp.account_locked = False
        await s.commit()
    return {"done": True, "unlocked": True}
