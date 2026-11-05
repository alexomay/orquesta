import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.llm.fake_llm import FakeLLM
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        llm_mode="fake",
        fake_latency_s=0,
        approval_timeout_s=10,
        _env_file=None,
    )


@pytest.fixture
async def app(settings):
    application = create_app(settings, llm=FakeLLM(latency_s=0))
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def wait_for(predicate, interval: float = 0.02):
    """Espera (máximo 5 s) hasta que predicate() (async) devuelva algo verdadero."""
    async with asyncio.timeout(5):
        while not (value := await predicate()):
            await asyncio.sleep(interval)
        return value
