# Orquesta

**Plataforma multi-agente de IA para mesa de ayuda de TI**, con aprobación humana para acciones sensibles, eventos en tiempo real y control de costos por ticket.

[![CI](https://github.com/alexomay/orquesta/actions/workflows/ci.yml/badge.svg)](https://github.com/alexomay/orquesta/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Claude](https://img.shields.io/badge/LLM-Claude%20Opus%205.5-orange)

Cuando llega un ticket, tres agentes trabajan en cadena:

1. **Triage**: lo clasifica, asigna prioridad y detecta riesgos como la ingeniería social.
2. **Resolver**: investiga con herramientas (base de conocimiento, estado de servicios, inventario, perfil del empleado) y actúa. Las acciones sensibles, como restablecer una contraseña, **se pausan hasta que un operador humano las aprueba**.
3. **Revisor QA**: califica la respuesta antes de enviarla. Si no la aprueba, el Resolver la corrige.

Todo se ve en vivo en el panel web: el razonamiento resumido de cada agente, las herramientas que llama, sus resultados, los tokens y el costo en dólares.

![Aprobación humana](docs/aprobacion-humana.png)

## Qué demuestra este proyecto

| Tema | Implementación |
|---|---|
| **IA agéntica** | Ciclo de herramientas propio sobre la API de Claude, con llamadas en paralelo, esquemas estrictos (`strict: true`) y salidas estructuradas (JSON Schema) |
| **Humano en el ciclo** | Las herramientas declaran su riesgo; las sensibles crean una aprobación y la ejecución espera la decisión, con tiempo de expiración |
| **Seguridad** | El texto del ticket se trata como datos no confiables. Hay defensa contra *prompt injection* y una política de "solo el titular" para cambios de credenciales. La aprobación puede protegerse con un token |
| **Tiempo real** | Bus de eventos asíncrono que manda cada paso por WebSocket; los eventos también se guardan para reconstruir el historial |
| **Costos** | Tokens y costo en USD por llamada, por ejecución y en promedio, calculados con el modelo que realmente respondió; caché de prompts activada |
| **Resiliencia** | Respaldo automático de modelo del lado del servidor si un clasificador rechaza la petición; recuperación de ejecuciones interrumpidas al reiniciar; límites de turnos |
| **Backend** | FastAPI asíncrono, SQLAlchemy 2.0 (SQLite o PostgreSQL), Pydantic v2, configuración por entorno |
| **Calidad** | 17 pruebas con pytest (flujos de punta a punta, API y unidades), ruff y CI en GitHub Actions que también construye y arranca la imagen de Docker |
| **Modo demo** | Un LLM simulado y determinista permite correr la demo y las pruebas sin API key ni costo |

## Arquitectura

```mermaid
flowchart LR
    UI[Panel web] -- REST --> API[FastAPI]
    UI <-- WebSocket --> BUS[Bus de eventos]
    API --> ORQ[Orquestador]
    ORQ --> T[Agente Triage]
    ORQ --> R[Agente Resolver]
    ORQ --> Q[Revisor QA]
    T & R & Q --> LLM{{Claude API<br/>o LLM simulado}}
    R --> TOOLS[Herramientas]
    TOOLS -->|acción sensible| APR[Aprobación humana]
    APR -. decisión .-> API
    ORQ --> BUS
    ORQ --> DB[(SQLite / PostgreSQL)]
    TOOLS --> DB
```

```
app/
├── agents/
│   ├── orchestrator.py   # Triage → Resolver (ciclo de herramientas) → Revisor, aprobaciones, costos, eventos
│   ├── tools.py          # Registro de herramientas con esquema Pydantic y nivel de riesgo
│   └── prompts.py        # Prompts de sistema y esquemas de salida
├── llm/
│   ├── anthropic_llm.py  # Cliente de Claude (SDK oficial)
│   └── fake_llm.py       # LLM simulado para demo, pruebas y CI
├── api.py                # Endpoints REST y WebSocket
├── models.py             # Modelos de SQLAlchemy
├── pricing.py            # Costo por modelo
└── static/index.html     # Panel en vivo (HTML + JS, sin build)
tests/                    # pytest: flujos, API y unidades
```

## Correrlo en 1 minuto

Requiere Python 3.12+.

```bash
git clone https://github.com/alexomay/orquesta.git
cd orquesta
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -e ".[dev]"
uvicorn app.main:create_app --factory --reload
```

Abre http://localhost:8000, elige un ticket de ejemplo y envíalo. La documentación interactiva de la API está en http://localhost:8000/docs.

**Con Claude real:** copia `.env.example` como `.env` y pon tu `ANTHROPIC_API_KEY`. Sin la key corre en modo demo.

**Con Docker y PostgreSQL:**

```bash
docker compose up --build
```

**Pruebas:**

```bash
pytest -q
ruff check . && ruff format --check .
```

## Escenarios de demo

| Ejemplo | Qué pasa |
|---|---|
| **VPN no conecta** | El Resolver consulta en paralelo la base de conocimiento, el perfil y el estado de la VPN; detecta un incidente activo y responde con los pasos y el aviso del incidente |
| **Cuenta bloqueada** | El Resolver pide `reset_password` y la ejecución **se pausa** hasta que un operador aprueba o deniega en el panel. Si se deniega, escala a nivel 2 |
| **Intento de ingeniería social** | El ticket pide restablecer la contraseña del director e intenta dar órdenes a la IA. Triage lo marca como riesgo crítico y el Resolver no toca la cuenta: deja una nota interna y escala a Seguridad |
| **Falla de hardware** | Revisa el inventario del empleado y turna a Soporte en sitio |

![Ingeniería social bloqueada](docs/ingenieria-social.png)

## Decisiones de diseño

- **Ciclo de herramientas propio en vez de un framework.** Así es posible pausar a media ejecución para una aprobación, emitir un evento por cada paso y medir el costo de cada llamada. Las herramientas independientes corren en paralelo con `asyncio.gather` y sus resultados regresan juntos en un solo mensaje.
- **El historial solo crece.** Nunca se editan mensajes anteriores: la retroalimentación del revisor entra como un mensaje nuevo. Eso mantiene válidos los bloques de razonamiento y permite reutilizar la caché de prompts.
- **El riesgo vive en la herramienta, no en el prompt.** `reset_password` está marcada como `requires_approval=True`. Aunque un *prompt injection* convenciera al modelo, la acción no se ejecuta sin un humano.
- **Prompts y lista de herramientas constantes** (sin fechas ni IDs, en orden estable), para que la caché de prompts funcione entre llamadas.
- **Proveedor de LLM intercambiable** detrás de un protocolo (`LLM.complete`). El simulado produce los mismos bloques que la API real, así las pruebas cubren el orquestador completo sin red.
- **Salidas estructuradas** para Triage y Revisor (JSON Schema), y esquemas estrictos en las herramientas: la entrada se valida otra vez con Pydantic antes de ejecutar.

## API

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/tickets` | Crea un ticket e inicia los agentes |
| `GET` | `/api/tickets` · `/api/tickets/{id}` | Lista y detalle (ejecuciones, notas, aprobaciones) |
| `POST` | `/api/tickets/{id}/runs` | Vuelve a ejecutar los agentes |
| `GET` | `/api/runs/{id}` | Ejecución con todos sus eventos |
| `GET` | `/api/approvals?status=pending` | Aprobaciones |
| `POST` | `/api/approvals/{id}` | `{"decision": "approve" \| "deny", "reason": "..."}` |
| `GET` | `/api/metrics` | Tasa de autorresolución, costo y tiempo promedio, tokens |
| `WS` | `/ws/events` | Eventos en tiempo real |

## Próximos pasos

- Búsqueda semántica (RAG) con pgvector sobre la base de conocimiento.
- Migraciones con Alembic y autenticación con roles (JWT).
- Trazas con OpenTelemetry y un set de evaluaciones para medir la calidad de los agentes.
- Integración con correo o Microsoft Teams como canal de entrada.

## Licencia

MIT
