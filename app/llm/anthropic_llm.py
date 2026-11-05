"""Proveedor real: Claude vía el SDK oficial de Anthropic."""

import time

import anthropic

from .base import LLMError, LLMRequest, LLMResult, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        # Sin api_key explícita, el SDK resuelve credenciales del entorno.
        self.client = client or (
            anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        )

    async def complete(self, req: LLMRequest) -> LLMResult:
        output_config: dict = {"effort": req.effort}
        if req.output_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.output_schema}

        kwargs: dict = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": req.messages,
            # Resúmenes del razonamiento para mostrarlos en la línea de tiempo.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": output_config,
            # Si un clasificador de seguridad rechaza la petición, la API reintenta con
            # otro modelo dentro de la misma llamada.
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if req.tools:
            kwargs["tools"] = req.tools
        if req.cache:
            kwargs["cache_control"] = {"type": "ephemeral"}

        started = time.perf_counter()
        try:
            resp = await self.client.beta.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            raise LLMError(f"Petición inválida para Claude: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("API key de Anthropic inválida o ausente") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Límite de uso de la API alcanzado; intenta más tarde") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Error de la API de Claude ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("No hay conexión con la API de Claude") from e
        latency_ms = int((time.perf_counter() - started) * 1000)

        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        stop_details = resp.stop_details.to_dict() if getattr(resp, "stop_details", None) else None
        return LLMResult(
            # Bloques sin modificar: el historial debe reenviarse tal cual (thinking incluido).
            content=[block.to_dict() for block in resp.content],
            stop_reason=resp.stop_reason,
            usage=usage,
            model=resp.model,
            latency_ms=latency_ms,
            stop_details=stop_details,
            extra={"request_id": getattr(resp, "_request_id", None)},
        )
