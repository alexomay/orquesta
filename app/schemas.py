from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _as_utc(d: datetime) -> datetime:
    # SQLite no guarda la zona horaria; todas las fechas se escriben en UTC.
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=5, max_length=4000)
    requester_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)


class TicketOut(ORM):
    id: int
    subject: str
    body: str
    requester_email: str
    status: str
    category: str | None
    priority: str | None
    summary: str | None
    risk_flags: list[str]
    reply: str | None
    escalated_to: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class NoteOut(ORM):
    id: int
    author: str
    text: str
    created_at: UtcDatetime


class RunOut(ORM):
    id: int
    ticket_id: int
    status: str
    llm_mode: str
    model: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    error: str | None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None


class EventOut(ORM):
    id: int
    run_id: int
    seq: int
    agent: str
    type: str
    payload: dict[str, Any]
    created_at: UtcDatetime


class ApprovalOut(ORM):
    id: int
    run_id: int
    ticket_id: int
    tool_name: str
    tool_input: dict[str, Any]
    status: str
    reason: str | None
    decided_by: str | None
    created_at: UtcDatetime
    decided_at: UtcDatetime | None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = Field(default=None, max_length=500)
    decided_by: str = Field(default="operador", max_length=100)


class TicketDetail(BaseModel):
    ticket: TicketOut
    runs: list[RunOut]
    notes: list[NoteOut]
    approvals: list[ApprovalOut]


class RunDetail(BaseModel):
    run: RunOut
    events: list[EventOut]


class TicketCreated(BaseModel):
    ticket: TicketOut
    run_id: int
