"""Costo en USD por llamada, según el modelo que realmente respondió (puede ser un fallback)."""

from .llm.base import Usage

# (entrada, salida, lectura de caché) en USD por millón de tokens.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    # El LLM simulado se cotiza como Opus 5.5 para que la demo muestre costos realistas.
    "fake-llm": (4.00, 20.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25  # escritura de caché con TTL de 5 minutos


def price_for(model: str) -> tuple[float, float, float]:
    return PRICES.get(model, PRICES["claude-opus-5-5"])


def cost_usd(model: str, usage: Usage) -> float:
    inp, out, cache_read = price_for(model)
    total = (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER
    )
    return round(total / 1_000_000, 6)
