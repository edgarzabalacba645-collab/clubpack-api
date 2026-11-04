"""
Zona de espera en memoria para archivos ya transcodificados/analizados
que todavía no se confirmaron para subir.

Refleja el flujo de 2 pasos que ya tenía la página "Subir Track" en
Streamlit (analizar → mostrar métricas y avisos de duplicado → confirmar),
adaptado a una API sin estado entre requests: /tracks/analyze deja el
MP3 ya convertido acá, /tracks/confirm lo retira y lo sube a R2.

Limitación a propósito: vive en memoria de un solo proceso. Para 20-30
DJs y un solo worker (ver README de deploy) es más que suficiente y
evita la complejidad de un disco compartido o Redis. Si el backend
llega a correr con varios workers/réplicas, esto hay que moverlo a
algo compartido (Redis, un bucket temporal en R2, etc.).
"""

import time
import threading
from dataclasses import dataclass, field
from uuid import uuid4

from .config import settings


@dataclass
class ArchivoEnEspera:
    contenido_mp3: bytes
    nombre_original: str
    usuario_id: str
    expira_en: float = field(default_factory=lambda: time.time() + settings.STAGING_TTL_SEG)


_ESPERA: dict[str, ArchivoEnEspera] = {}
_LOCK = threading.Lock()


def _purgar_vencidos():
    ahora = time.time()
    vencidos = [k for k, v in _ESPERA.items() if v.expira_en < ahora]
    for k in vencidos:
        _ESPERA.pop(k, None)


def guardar(contenido_mp3: bytes, nombre_original: str, usuario_id: str) -> str:
    with _LOCK:
        _purgar_vencidos()
        staging_id = str(uuid4())
        _ESPERA[staging_id] = ArchivoEnEspera(
            contenido_mp3=contenido_mp3,
            nombre_original=nombre_original,
            usuario_id=usuario_id,
        )
        return staging_id


def retirar(staging_id: str, usuario_id: str) -> ArchivoEnEspera | None:
    """Saca (y elimina) la entrada, solo si pertenece al mismo usuario que la creó."""
    with _LOCK:
        _purgar_vencidos()
        entrada = _ESPERA.get(staging_id)
        if entrada is None or entrada.usuario_id != usuario_id:
            return None
        del _ESPERA[staging_id]
        return entrada
