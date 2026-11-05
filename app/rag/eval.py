"""Evaluación de la búsqueda: ¿el artículo correcto aparece entre los primeros resultados?

Las consultas están escritas como las escribiría un empleado (paráfrasis, sin las palabras
exactas del artículo), que es donde una búsqueda ingenua falla.

Uso:  python -m app.rag.eval
"""

import asyncio
from dataclasses import dataclass

from .retriever import KBDoc, Retriever

EVAL_SET: list[tuple[str, str]] = [
    ("no puedo entrar a los sistemas de la empresa desde mi casa", "VPN no conecta desde casa"),
    (
        "se me olvidó la clave y ya no me deja iniciar sesión",
        "Cuenta bloqueada o contraseña olvidada",
    ),
    ("outlook no me baja los correos nuevos", "Outlook no sincroniza el correo"),
    ("mandé a imprimir y no sale nada", "La impresora no imprime"),
    ("necesito permiso para el módulo de compras en SAP", "Acceso a módulos del ERP"),
    ("la computadora está muy lenta", "Laptop lenta"),
    ("la pantalla se pone negra a cada rato", "Pantalla o hardware dañado"),
    (
        "me llegó un correo raro con un enlace para verificar mi cuenta",
        "Correo sospechoso o phishing",
    ),
    (
        "en las reuniones de teams no me escuchan",
        "Teams: no se escucha o no se ve en las reuniones",
    ),
    ("mis archivos no se suben a la nube", "OneDrive no sincroniza archivos"),
    ("no veo la carpeta compartida de finanzas", "Acceso a carpetas compartidas"),
    (
        "cambié de celular y ya no me llega el código de verificación",
        "Autenticación en dos pasos (MFA)",
    ),
    ("quiero que me instalen un programa para editar video", "Solicitar software nuevo"),
    ("conecté un segundo monitor por hdmi y no da imagen", "Monitor externo no detecta imagen"),
    (
        "me piden cambiar la contraseña de otro usuario",
        "Política: cambios de credenciales de terceros",
    ),
    ("el lunes entra una persona nueva a mi equipo y necesita laptop", "Alta de empleado nuevo"),
    ("quiero ver el correo de la empresa en mi iphone", "Correo en el celular corporativo"),
]


@dataclass
class EvalReport:
    mode: str
    total: int
    recall_at_1: float
    recall_at_3: float
    mrr: float
    misses: list[tuple[str, str, list[str]]]


async def evaluate(retriever: Retriever, k: int = 3) -> EvalReport:
    hits1 = hits_k = 0
    rr = 0.0
    misses = []
    for query, expected in EVAL_SET:
        titles = [h.title for h in await retriever.search(query, k=10)]
        if titles[:1] == [expected]:
            hits1 += 1
        if expected in titles[:k]:
            hits_k += 1
        else:
            misses.append((query, expected, titles[:k]))
        if expected in titles:
            rr += 1 / (titles.index(expected) + 1)
    n = len(EVAL_SET)
    return EvalReport(retriever.mode, n, hits1 / n, hits_k / n, rr / n, misses)


async def _main() -> None:
    from ..seed import KB

    retriever = Retriever()
    await retriever.index([KBDoc(i, t, tags, c) for i, (t, tags, c) in enumerate(KB, 1)])
    r = await evaluate(retriever)
    print(f"Búsqueda: {r.mode} · {r.total} consultas")
    print(f"recall@1 = {r.recall_at_1:.2f}   recall@3 = {r.recall_at_3:.2f}   MRR = {r.mrr:.2f}")
    for query, expected, got in r.misses:
        print(f"  ✗ «{query}» esperaba «{expected}», obtuvo {got}")


if __name__ == "__main__":
    asyncio.run(_main())
