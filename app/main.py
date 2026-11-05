"""Punto de entrada: `uvicorn app.main:create_app --factory`."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update

from . import __version__
from .agents.orchestrator import ApprovalBroker, Orchestrator
from .api import router, ws_router
from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessions
from .events import EventBus
from .llm import LLM, build_llm
from .models import Approval, Run, Ticket, utcnow
from .rag import Retriever
from .rag.embeddings import build_embedder
from .seed import seed

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("orquesta")


async def recover_interrupted(sessions) -> None:
    """Las ejecuciones que quedaron a medias en un reinicio se marcan como fallidas."""
    active = ["queued", "running", "awaiting_approval"]
    async with sessions() as s:
        await s.execute(
            update(Run)
            .where(Run.status.in_(active))
            .values(
                status="failed",
                error="Interrumpida por reinicio del servidor",
                finished_at=utcnow(),
            )
        )
        await s.execute(
            update(Approval)
            .where(Approval.status == "pending")
            .values(status="expired", decided_at=utcnow())
        )
        await s.execute(
            update(Ticket)
            .where(Ticket.status.in_(["new", "triaged", "in_progress", "awaiting_approval"]))
            .values(status="failed")
        )
        await s.commit()


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await init_db(engine)
        sessions = make_sessions(engine)
        if settings.seed_demo:
            await seed(sessions)
        await recover_interrupted(sessions)

        retriever = Retriever(build_embedder(settings.voyage_api_key))
        await retriever.index_from_db(sessions)

        bus, broker = EventBus(), ApprovalBroker()
        orchestrator = Orchestrator(
            sessions=sessions,
            llm=llm or build_llm(settings),
            bus=bus,
            broker=broker,
            settings=settings,
            retriever=retriever,
        )
        app.state.settings = settings
        app.state.sessions = sessions
        app.state.bus = bus
        app.state.broker = broker
        app.state.orchestrator = orchestrator
        app.state.retriever = retriever
        log.info(
            "Orquesta %s lista · LLM: %s (%s) · búsqueda: %s",
            __version__,
            orchestrator.llm.name,
            orchestrator.llm.model,
            retriever.mode,
        )
        yield
        await orchestrator.shutdown()
        await engine.dispose()

    app = FastAPI(
        title="Orquesta",
        version=__version__,
        description="Plataforma multi-agente de IA para mesa de ayuda de TI.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(ws_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
