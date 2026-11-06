"""
Zona de espera en disco para archivos ya transcodificados/analizados
que todavía no se confirmaron para subir.

Refleja el flujo de 2 pasos que ya tenía la página "Subir Track" en
Streamlit (analizar → mostrar métricas y avisos de duplicado → confirmar),
adaptado a una API sin estado entre requests: /tracks/analyze deja el
MP3 ya convertido acá, /tracks/confirm lo retira y lo sube a R2.

Antes esto vivía en memoria (los bytes del MP3 en un dict del proceso).
Con la carga masiva de discos completos, tener varios temas en espera al
mismo tiempo — cada uno ya transcodificado a 320kbps, varios MB — podía
sumar memoria rápido en una instancia chica, sobre todo si el usuario
tarda en revisar/confirmar. Por eso ahora el archivo se escribe a un
directorio temporal del propio proceso y en el dict solo se guarda la
ruta: la memoria del proceso no crece con la cantidad de temas en espera,
solo el disco (mucho más abundante que la RAM en estos planes chicos).

Sigue siendo, a propósito, un solo proceso: para 20-30 DJs y un solo
worker (ver README de deploy) alcanza. Si el backend llega a correr con
varios workers/réplicas, esto hay que moverlo a algo compartido (Redis,
un bucket temporal en R2, etc.) porque cada proceso tendría su propio
directorio y no se verían entre sí.
"""

import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .config import settings

logger = logging.getLogger("clubpack.staging")

_DIR_ESPERA = Path(tempfile.gettempdir()) / "clubpack_staging"
# Directorio limpio en cada arranque del proceso: lo que haya quedado de
# una corrida anterior (crash, redeploy) ya no tiene dueño — el dict en
# memoria también arranca vacío — así que es basura huérfana en disco.
shutil.rmtree(_DIR_ESPERA, ignore_errors=True)
_DIR_ESPERA.mkdir(parents=True, exist_ok=True)


@dataclass
class ArchivoEnEspera:
    ruta_mp3: Path
    nombre_original: str
    usuario_id: str
    expira_en: float = field(default_factory=lambda: time.time() + settings.STAGING_TTL_SEG)

    @property
    def contenido_mp3(self) -> bytes:
        return self.ruta_mp3.read_bytes()


_ESPERA: dict[str, ArchivoEnEspera] = {}
_LOCK = threading.Lock()


def _borrar_archivo(ruta: Path):
    try:
        ruta.unlink()
    except OSError:
        pass


def _purgar_vencidos():
    ahora = time.time()
    vencidos = [k for k, v in _ESPERA.items() if v.expira_en < ahora]
    for k in vencidos:
        entrada = _ESPERA.pop(k, None)
        if entrada:
            _borrar_archivo(entrada.ruta_mp3)


def guardar(contenido_mp3: bytes, nombre_original: str, usuario_id: str) -> str:
    with _LOCK:
        _purgar_vencidos()
        staging_id = str(uuid4())
        ruta = _DIR_ESPERA / f"{staging_id}.mp3"
        ruta.write_bytes(contenido_mp3)
        _ESPERA[staging_id] = ArchivoEnEspera(
            ruta_mp3=ruta,
            nombre_original=nombre_original,
            usuario_id=usuario_id,
        )
        return staging_id


def retirar(staging_id: str, usuario_id: str) -> ArchivoEnEspera | None:
    """
    Saca (y elimina del dict) la entrada, solo si pertenece al mismo
    usuario que la creó. OJO: esto NO borra el archivo de disco — eso lo
    hace quien llama, con `borrar()`, recién cuando termina de usarlo
    (/tracks/confirm lo necesita leer varias veces antes de poder
    descartarlo).
    """
    with _LOCK:
        _purgar_vencidos()
        entrada = _ESPERA.get(staging_id)
        if entrada is None or entrada.usuario_id != usuario_id:
            return None
        del _ESPERA[staging_id]
        return entrada


def borrar(entrada: ArchivoEnEspera):
    """Descarta el archivo temporal de una entrada ya retirada."""
    _borrar_archivo(entrada.ruta_mp3)
