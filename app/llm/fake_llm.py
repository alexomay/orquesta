"""LLM simulado y determinista para la demo, las pruebas y la CI.

Imita el comportamiento de los agentes con reglas sencillas y produce bloques con el mismo
formato que la API de Claude (thinking, text, tool_use), así el resto del sistema corre igual
con o sin API key.
"""

import asyncio
import itertools
import json
import re
import time
import unicodedata
from typing import Any

from .base import LLMRequest, LLMResult, Usage

_ids = itertools.count(1)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _tokens(obj: Any) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)


def _first_user_text(messages: list[dict]) -> str:
    content = messages[0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def _thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": text, "signature": "simulated"}


def _tool_use(name: str, **inp: Any) -> dict:
    return {"type": "tool_use", "id": f"toolu_sim_{next(_ids):05d}", "name": name, "input": inp}


SERVICE_HINTS = {
    "vpn": "VPN",
    "forticlient": "VPN",
    "outlook": "Correo",
    "correo": "Correo",
    "sap": "ERP",
    "erp": "ERP",
    "wifi": "WiFi",
    "impresora": "Impresión",
    "imprimir": "Impresión",
}

_STOP_TEXT = (
    "el la los las un una de del en y o que mi me no se por para con al es lo ya hoy "
    "favor ayuda hola buen dia buenos dias tengo puedo desde esta este"
)
STOPWORDS = frozenset(_STOP_TEXT.split())


class FakeLLM:
    name = "fake"
    model = "fake-llm"

    def __init__(self, latency_s: float = 0.7) -> None:
        self.latency_s = latency_s

    async def complete(self, req: LLMRequest) -> LLMResult:
        started = time.perf_counter()
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        handler = {"triage": self._triage, "resolver": self._resolver, "reviewer": self._reviewer}
        content, stop_reason = handler[req.agent](req)
        usage = Usage(
            input_tokens=_tokens(req.system) + _tokens(req.messages) + _tokens(req.tools or []),
            output_tokens=_tokens(content),
        )
        return LLMResult(
            content=content,
            stop_reason=stop_reason,
            usage=usage,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- Triage --------------------------------------------------------------------------

    def _triage(self, req: LLMRequest) -> tuple[list[dict], str]:
        text = norm(_first_user_text(req.messages))
        if any(k in text for k in ("contrasena", "password", "bloque", "acceso", "login")):
            category = "acceso"
        elif any(k in text for k in ("vpn", "wifi", "internet", "red ")):
            category = "red"
        elif any(k in text for k in ("laptop", "pantalla", "teclado", "mouse", "impresora")):
            category = "hardware"
        elif any(k in text for k in ("outlook", "excel", "sap", "erp", "instalar")):
            category = "software"
        else:
            category = "otro"

        flags = []
        if ("ignora" in text and "instrucciones" in text) or re.search(
            r"contrasena de [a-z.]+@", text
        ):
            flags.append("ingenieria_social")
        if "director" in text:
            flags.append("vip")

        if "ingenieria_social" in flags or "caido" in text:
            priority = "critica"
        elif any(k in text for k in ("urgente", "no puedo trabajar", "no me deja")):
            priority = "alta"
        elif "cuando puedas" in text:
            priority = "baja"
        else:
            priority = "media"

        subject = re.search(r"Asunto: (.+)", _first_user_text(req.messages))
        summary = subject.group(1).strip() if subject else "Solicitud de soporte"
        result = {
            "category": category,
            "priority": priority,
            "summary": summary,
            "sentiment": "urgente" if priority in ("alta", "critica") else "tranquilo",
            "risk_flags": flags,
        }
        content = [
            _thinking(f"El ticket trata de {category}; asigno prioridad {priority}."),
            {"type": "text", "text": json.dumps(result, ensure_ascii=False)},
        ]
        return content, "end_turn"

    # --- Resolver -----------------------------------------------------------------------

    def _resolver(self, req: LLMRequest) -> tuple[list[dict], str]:
        first = _first_user_text(req.messages)
        text = norm(first)
        requester = (re.search(r"Solicitante: (\S+@\S+)", first) or [None, None])[1]
        triage_match = re.search(r"<triage>(.*?)</triage>", first, re.S)
        triage = json.loads(triage_match.group(1)) if triage_match else {}
        category = triage.get("category", "otro")
        flags = triage.get("risk_flags", [])

        calls: dict[str, dict] = {}  # nombre -> {"input":..., "result":..., "is_error":...}
        by_id: dict[str, str] = {}
        for msg in req.messages:
            if not isinstance(msg["content"], list):
                continue
            for b in msg["content"]:
                if b.get("type") == "tool_use":
                    by_id[b["id"]] = b["name"]
                    calls[b["name"]] = {"input": b["input"]}
                elif b.get("type") == "tool_result" and b["tool_use_id"] in by_id:
                    name = by_id[b["tool_use_id"]]
                    calls[name]["result"] = b.get("content", "")
                    calls[name]["is_error"] = bool(b.get("is_error"))

        def result(name: str) -> dict:
            try:
                return json.loads(calls[name]["result"])
            except (KeyError, ValueError, TypeError):
                return {}

        def step(thought: str, *tools: dict, say: str | None = None) -> tuple[list[dict], str]:
            content: list[dict] = [_thinking(thought)]
            if say:
                content.append({"type": "text", "text": say})
            content.extend(tools)
            return content, "tool_use"

        def final(reply: str) -> tuple[list[dict], str]:
            return [
                _thinking("Tengo lo necesario; redacto la respuesta."),
                {"type": "text", "text": reply},
            ], "end_turn"

        # 1) Investigar: varias herramientas en paralelo.
        if not calls:
            keywords = [w for w in re.findall(r"[a-z0-9]+", text) if w not in STOPWORDS]
            tools = [_tool_use("search_knowledge_base", query=" ".join(keywords[:8]))]
            if requester:
                tools.append(_tool_use("get_employee", email=requester))
            service = next((v for k, v in SERVICE_HINTS.items() if k in text), None)
            if service:
                tools.append(_tool_use("check_service_status", service=service))
            return step(
                "Primero reviso la base de conocimiento, el perfil del solicitante y el estado "
                "del servicio relacionado.",
                *tools,
                say="Reviso la información disponible.",
            )

        employee = result("get_employee")
        name = (employee.get("name") or "").split(" ")[0] or "Hola"
        greeting = f"Hola {name}," if name != "Hola" else "Hola,"
        kb = (result("search_knowledge_base").get("results") or [{}])[0]

        # 2) Posible ingeniería social: no actuar, documentar y escalar a Seguridad.
        if "ingenieria_social" in flags:
            if "escalate_to_human" not in calls:
                return step(
                    "El ticket pide actuar sobre la cuenta de otra persona e intenta darme "
                    "instrucciones. Sigo la política de seguridad: no hago el cambio y escalo.",
                    _tool_use(
                        "add_internal_note",
                        note="Posible ingeniería social: se solicita reset de contraseña de un "
                        "tercero (cuenta VIP). No se ejecutó ninguna acción.",
                    ),
                    _tool_use(
                        "escalate_to_human",
                        team="Seguridad",
                        reason="Solicitud de cambio de credenciales de un tercero.",
                    ),
                )
            return final(
                f"{greeting} por política de seguridad solo podemos restablecer la contraseña de "
                "la persona dueña de la cuenta, y siempre verificando su identidad. No realizamos "
                "ningún cambio. Turnamos tu solicitud al equipo de Seguridad, que te contactará. "
                "Si la persona titular necesita acceso, pídele que abra un ticket desde su cuenta "
                "o que llame a la extensión 4000."
            )

        # 3) Acceso: reset con aprobación humana.
        if category == "acceso" and requester:
            if "reset_password" not in calls:
                return step(
                    "La cuenta del solicitante está bloqueada. El reset requiere aprobación de "
                    "un operador.",
                    _tool_use(
                        "reset_password",
                        email=requester,
                        reason="Cuenta bloqueada por intentos fallidos; lo solicita el titular.",
                    ),
                )
            if calls["reset_password"].get("is_error"):
                if "escalate_to_human" not in calls:
                    return step(
                        "El operador denegó el reset. No insisto; escalo a nivel 2.",
                        _tool_use(
                            "escalate_to_human",
                            team="Mesa de ayuda N2",
                            reason="Reset de contraseña denegado por el operador.",
                        ),
                    )
                return final(
                    f"{greeting} revisamos tu cuenta y el restablecimiento necesita una "
                    "validación adicional. Turnamos tu caso a la mesa de ayuda de nivel 2; te "
                    "contactarán en breve para verificar tu identidad. Mientras tanto no intentes "
                    "iniciar sesión de nuevo para no extender el bloqueo."
                )
            return final(
                f"{greeting} desbloqueamos tu cuenta y enviamos un enlace para crear una nueva "
                "contraseña a tu celular registrado.\n1. Abre el enlace (vence en 30 minutos).\n"
                "2. Crea una contraseña de al menos 12 caracteres.\n3. Inicia sesión de nuevo en "
                "tu equipo y en Outlook.\nSi el enlace no llega en 5 minutos, responde a este "
                "ticket."
            )

        # 4) Hardware: requiere a una persona en sitio.
        if category == "hardware":
            if "escalate_to_human" not in calls:
                return step(
                    "Es una falla física; necesita revisión en sitio.",
                    _tool_use("list_employee_assets", email=requester or ""),
                    _tool_use(
                        "escalate_to_human",
                        team="Soporte en sitio",
                        reason=triage.get("summary", "Falla de hardware"),
                    ),
                )
            return final(
                f"{greeting} tu caso requiere revisión física del equipo, así que lo asignamos a "
                "Soporte en sitio; un técnico te contactará hoy para agendar la visita. Mientras "
                "tanto, respalda en OneDrive los archivos importantes."
            )

        # 5) Red / software / otros: respuesta con la base de conocimiento.
        service = result("check_service_status")
        incident = ""
        if service.get("status") in ("degradado", "caido"):
            incident = (
                f"Además, el servicio {service.get('name')} reporta un incidente "
                f"({service.get('note')}); ya está en atención. "
            )
        steps = kb.get("content", "Reinicia el equipo e intenta de nuevo.")
        return final(
            f"{greeting} revisamos tu caso. {incident}Sigue estos pasos de nuestra guía "
            f'"{kb.get("title", "Soporte")}":\n{steps}\nSi después de esto el problema '
            "continúa, responde a este ticket y lo atendemos de inmediato."
        )

    # --- Revisor ------------------------------------------------------------------------

    def _reviewer(self, req: LLMRequest) -> tuple[list[dict], str]:
        body = _first_user_text(req.messages)
        m = re.search(r"<respuesta_propuesta>(.*?)</respuesta_propuesta>", body, re.S)
        reply = (m.group(1) if m else "").strip()
        leaks = re.search(r"contrase[ñn]a (temporal|nueva) es", norm(reply))
        ok = len(reply) >= 60 and not leaks
        review = {
            "approved": ok,
            "score": 9 if ok else 4,
            "issues": [] if ok else ["La respuesta es muy corta o expone datos sensibles."],
            "suggested_fix": "" if ok else "Explica qué se hizo y los siguientes pasos.",
        }
        content = [
            _thinking("Verifico tono, exactitud y que no se expongan datos sensibles."),
            {"type": "text", "text": json.dumps(review, ensure_ascii=False)},
        ]
        return content, "end_turn"
