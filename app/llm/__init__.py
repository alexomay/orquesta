from ..config import Settings
from .anthropic_llm import AnthropicLLM
from .base import LLM, LLMError, LLMRequest, LLMResult, Usage
from .fake_llm import FakeLLM

__all__ = ["LLM", "LLMError", "LLMRequest", "LLMResult", "Usage", "build_llm"]


def build_llm(settings: Settings) -> LLM:
    if settings.resolved_llm_mode() == "anthropic":
        return AnthropicLLM(model=settings.model, api_key=settings.anthropic_api_key)
    return FakeLLM(latency_s=settings.fake_latency_s)
