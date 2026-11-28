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

`guardar()` recibe la ruta del MP3 ya transcodificado (ver
app/routers/tracks.py) y lo MUEVE a este directorio de espera — nunca
pasa por memoria como bytes, ni siquiera para sesiones largas (~1h,
~200MB), que es justo el caso que antes podía tirar abajo el proceso por
quedarse sin RAM en el plan gratuito de Render.

El dict (`_ESPERA`) sigue siendo la fuente de verdad en caliente, pero
cada entrada también se escribe a un sidecar .json al lado de su .mp3
(ver `_escribir_meta`). Esto es lo que permite sobrevivir a un reinicio
del proceso (caída por falta de memoria, redeploy, o el propio ciclo de
sleep/wake del plan gratuito de Render) sin perder un análisis recién
hecho: antes este directorio se borraba entero al arrancar, así que
cualquier tema esperando confirmación en ese momento quedaba huérfano, y
/tracks/confirm respondía "no se encontró el análisis" como si hubiesen
pasado los 90 minutos del TTL aunque el reinicio pasara segundos después
de analizarlo — el proceso, no el vencimiento, era el que se había ido.
Ahora, al arrancar, `_restaurar_desde_disco()` reconstruye `_ESPERA` a
partir de esos sidecars (descartando los ya vencidos y cualquier .mp3
huérfano sin sidecar).

Sigue siendo, a propósito, un solo proceso: para 20-30 DJs y un solo
worker (ver README de deploy) alcanza. Si el backend llega a correr con
varios workers/réplicas, esto hay que moverlo a algo compartido (Redis,
un bucket temporal en R2, etc.) porque cada proceso tendría su propio
directorio y no se verían entre sí — y el sidecar en disco de acá no
alcanzaría tampoco (dos procesos verían el mismo directorio, pero cada
uno reconstruiría su propio dict en memoria al arrancar, pisándose).
"""

import json
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
_DIR_ESPERA.mkdir(parents=True, exist_ok=True)


@dataclass
class ArchivoEnEspera:
    ruta_mp3: Path
    nombre_original: str
    usuario_id: str
    # Guardado acá desde /tracks/analyze (que ya lo calculó) para que
    # /tracks/confirm no tenga que correr fpcalc una segunda vez sobre el
    # mismo archivo — con sesiones largas, cada corrida de más de un paso
    # pesado en la misma request es presupuesto de memoria/CPU que no
    # sobra en el plan gratis de Render (ver nota en app/audio.py).
    fingerprint: str | None = None
    # Si transcodificar_a_mp3_320 (ver app/audio.py) pudo nivelar el
    # volumen de este archivo — /tracks/confirm lo usa para guardar
    # tracks.loudness_lufs solo cuando corresponde (ver
    # sql/023_nivelado_volumen.sql). El archivo en sí (ruta_mp3) ya salió
    # nivelado de /analyze si esto es True; acá solo viaja el dato para
    # no tener que volver a medir el volumen una segunda vez.
    nivelado: bool = False
    expira_en: float = field(default_factory=lambda: time.time() + settings.STAGING_TTL_SEG)


_ESPERA: dict[str, ArchivoEnEspera] = {}
_LOCK = threading.Lock()


def _borrar_archivo(ruta: Path):
    try:
        ruta.unlink()
    except OSError:
        pass


def _ruta_meta(staging_id: str) -> Path:
    return _DIR_ESPERA / f"{staging_id}.json"


def _escribir_meta(staging_id: str, entrada: ArchivoEnEspera) -> None:
    """
    Sidecar en disco con los mismos datos de `entrada` (menos ruta_mp3,
    que se puede derivar del propio staging_id) para poder reconstruir
    `_ESPERA` si el proceso se reinicia antes de la confirmación — ver
    `_restaurar_desde_disco`. Best-effort a propósito: si esto falla (disco
    lleno, permisos) se loguea pero NO rompe la subida — el peor caso es
    el de siempre hasta ahora (no sobrevive un reinicio), no uno nuevo.
    """
    try:
        _ruta_meta(staging_id).write_text(
            json.dumps(
                {
                    "nombre_original": entrada.nombre_original,
                    "usuario_id": entrada.usuario_id,
                    "fingerprint": entrada.fingerprint,
                    "nivelado": entrada.nivelado,
                    "expira_en": entrada.expira_en,
                }
            )
        )
    except OSError:
        logger.warning("No se pudo guardar el sidecar de staging de %s (no va a sobrevivir a un reinicio).", staging_id)


def _purgar_vencidos():
    ahora = time.time()
    vencidos = [k for k, v in _ESPERA.items() if v.expira_en < ahora]
    for k in vencidos:
        entrada = _ESPERA.pop(k, None)
        if entrada:
            _borrar_archivo(entrada.ruta_mp3)
            _borrar_archivo(_ruta_meta(k))


def _restaurar_desde_disco() -> None:
    """
    Reconstruye `_ESPERA` a partir de los sidecars .json que hayan
    quedado en `_DIR_ESPERA` de una corrida anterior del proceso (ver
    docstring del módulo). Se llama una sola vez, al importar este
    módulo — es decir, al arrancar el proceso.

    Descarta (borrando del disco) cualquier sidecar vencido, corrupto, o
    sin su .mp3 correspondiente (puede pasar si el proceso se cortó justo
    a mitad de `guardar()`, entre mover el .mp3 y escribir el sidecar —
    una ventana de milisegundos). También barre al final cualquier .mp3
    suelto sin sidecar: basura huérfana real, no algo para reintentar.
    """
    ahora = time.time()
    restaurados = 0
    for ruta_meta in _DIR_ESPERA.glob("*.json"):
        staging_id = ruta_meta.stem
        ruta_mp3 = _DIR_ESPERA / f"{staging_id}.mp3"
        try:
            datos = json.loads(ruta_meta.read_text())
            expira_en = float(datos["expira_en"])
            usuario_id = str(datos["usuario_id"])
            if not ruta_mp3.exists() or expira_en < ahora:
                raise ValueError("vencido o sin archivo")
            _ESPERA[staging_id] = ArchivoEnEspera(
                ruta_mp3=ruta_mp3,
                nombre_original=datos.get("nombre_original") or "audio.mp3",
                usuario_id=usuario_id,
                fingerprint=datos.get("fingerprint"),
                nivelado=bool(datos.get("nivelado", False)),
                expira_en=expira_en,
            )
            restaurados += 1
        except Exception:
            _borrar_archivo(ruta_meta)
            _borrar_archivo(ruta_mp3)
    # .mp3 sin sidecar (ni vivo ni recién descartado arriba): no hay forma
    # de saber de quién era ni cuándo vence, así que no se puede recuperar.
    for ruta_mp3 in _DIR_ESPERA.glob("*.mp3"):
        if not _ruta_meta(ruta_mp3.stem).exists():
            _borrar_archivo(ruta_mp3)
    if restaurados:
        logger.info("staging: %d archivo(s) en espera recuperado(s) de una corrida anterior del proceso.", restaurados)


def guardar(
    ruta_mp3_origen: Path,
    nombre_original: str,
    usuario_id: str,
    fingerprint: str | None = None,
    nivelado: bool = False,
) -> str:
    """
    Mueve (no copia) el MP3 ya transcodificado desde su ubicación temporal
    de la request de /analyze hacia el directorio de espera — un solo
    archivo en disco en todo momento, nunca sus bytes duplicados en
    memoria de Python. Además deja un sidecar .json al lado (ver
    `_escribir_meta`) para poder reconstruir esta entrada si el proceso
    se reinicia antes de que se confirme.
    """
    with _LOCK:
        _purgar_vencidos()
        staging_id = str(uuid4())
        ruta = _DIR_ESPERA / f"{staging_id}.mp3"
        shutil.move(str(ruta_mp3_origen), str(ruta))
        entrada = ArchivoEnEspera(
            ruta_mp3=ruta,
            nombre_original=nombre_original,
            usuario_id=usuario_id,
            fingerprint=fingerprint,
            nivelado=nivelado,
        )
        _ESPERA[staging_id] = entrada
        _escribir_meta(staging_id, entrada)
        return staging_id


def retirar(staging_id: str, usuario_id: str) -> ArchivoEnEspera | None:
    """
    Saca (y elimina del dict, y de su sidecar en disco) la entrada, solo
    si pertenece al mismo usuario que la creó. OJO: esto NO borra el
    ARCHIVO de disco — eso lo hace quien llama, con `borrar()`, recién
    cuando termina de usarlo (/tracks/confirm lo necesita leer varias
    veces antes de poder descartarlo). El sidecar sí se borra acá ya
    mismo: a partir de este punto esta entrada queda "gastada" — igual
    que hoy, si confirmar() falla a mitad de camino después de esto, la
    recuperación es volver a subir el archivo, con o sin reinicio de por
    medio.
    """
    with _LOCK:
        _purgar_vencidos()
        entrada = _ESPERA.get(staging_id)
        if entrada is None or entrada.usuario_id != usuario_id:
            return None
        del _ESPERA[staging_id]
        _borrar_archivo(_ruta_meta(staging_id))
        return entrada


def borrar(entrada: ArchivoEnEspera):
    """Descarta el archivo temporal de una entrada ya retirada."""
    _borrar_archivo(entrada.ruta_mp3)


# Al importar este módulo (= al arrancar el proceso), reconstruye lo que
# haya quedado en disco de una corrida anterior, en vez de descartarlo a
# ciegas — ver docstring del módulo.
_restaurar_desde_disco()
