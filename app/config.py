from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración leída de variables de entorno (prefijo ORQUESTA_) o de .env."""

    model_config = SettingsConfigDict(env_prefix="ORQUESTA_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./orquesta.db"

    # auto = usa Claude si hay ANTHROPIC_API_KEY; si no, el LLM simulado (modo demo).
    llm_mode: Literal["auto", "anthropic", "fake"] = "auto"
    model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # Opcional: activa la búsqueda semántica (embeddings de Voyage AI) junto a BM25.
    voyage_api_key: str | None = Field(default=None, validation_alias="VOYAGE_API_KEY")

    fake_latency_s: float = 0.7
    max_agent_turns: int = 12
    max_review_rounds: int = 1
    approval_timeout_s: float = 3600

    # Si se define, aprobar o denegar acciones exige el header X-Admin-Token.
    admin_token: str | None = None
    seed_demo: bool = True

    def resolved_llm_mode(self) -> Literal["anthropic", "fake"]:
        if self.llm_mode == "auto":
            return "anthropic" if self.anthropic_api_key else "fake"
        return self.llm_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()
