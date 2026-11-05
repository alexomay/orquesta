"""Contrato común para los proveedores de LLM (Claude real o simulado).

El historial usa el formato de mensajes de la API de Claude (listas de bloques como dicts),
así el orquestador no depende del proveedor y el historial se puede guardar como JSON.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMRequest:
    agent: str  # triage | resolver | reviewer
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    output_schema: dict[str, Any] | None = None
    effort: str = "medium"
    max_tokens: int = 16000
    cache: bool = True


@dataclass
class LLMResult:
    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage
    model: str
    latency_ms: int
    stop_details: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def thinking_summaries(self) -> list[str]:
        return [
            b["thinking"].strip()
            for b in self.content
            if b.get("type") == "thinking" and (b.get("thinking") or "").strip()
        ]


class LLMError(RuntimeError):
    """Error de proveedor que ya agotó los reintentos del SDK."""


class LLM(Protocol):
    name: str
    model: str

    async def complete(self, req: LLMRequest) -> LLMResult: ...
