"""
Audio compartido en el chat de comunidad.

A propósito esto NO transcodea ni analiza nada (eso es solo para la
subida "oficial" al catálogo — ver /tracks/analyze): acá es nada más
"te mando este tema", para que el admin lo escuche y decida si lo sube
él al catálogo (ver sql/010_control_subidas.sql, pensado justo para
ese flujo cuando las subidas están cerradas para el resto). El archivo
se sube tal cual llega, directo a R2 — sin ffmpeg/librosa/fpcalc de por
medio, esto es mucho más liviano que /tracks/analyze.

El mensaje de chat en sí (texto + referencia al audio) lo inserta el
frontend directo contra Supabase, como cualquier otro mensaje (ver
ChatPage.tsx) — este backend solo hace la parte que necesita las
credenciales de R2.
"""

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import audio, storage
from ..deps import UsuarioAutenticado, requiere_acceso_activo, requiere_admin
from ..schemas import AnalisisTrackResponse
from .tracks import procesar_y_dejar_en_espera

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger("clubpack.chat")

EXTENSIONES_PERMITIDAS = {"mp3", "wav", "flac"}

# Tope propio del chat — más chico que MAX_UPLOAD_MB (subida oficial al
# catálogo, que además necesita margen para transcodificar). Esto es
# para compartir UN tema de punta a punta, alcanza de sobra incluso en
# WAV/FLAC sin comprimir.
MAX_AUDIO_CHAT_MB = 100
CHUNK_SIZE = 1024 * 1024


@router.post("/audio")
def subir_audio_chat(
    file: UploadFile,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    nombre_original = file.filename or "audio"
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else ""
    if extension not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(
            status_code=422,
            detail="Formato no soportado para el chat — subí mp3, wav o flac.",
        )

    max_bytes = MAX_AUDIO_CHAT_MB * 1024 * 1024
    tmp_dir = Path(tempfile.mkdtemp(prefix="clubpack_chataudio_"))
    try:
        ruta = tmp_dir / f"original.{extension}"
        total = 0
        # Mismo patrón que /tracks/analyze: nunca se junta el archivo
        # entero como bytes de Python, se escribe a disco en pedazos.
        with open(ruta, "wb") as destino:
            while True:
                pedazo = file.file.read(CHUNK_SIZE)
                if not pedazo:
                    break
                total += len(pedazo)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"El audio supera el máximo permitido de {MAX_AUDIO_CHAT_MB}MB para el chat.",
                    )
                destino.write(pedazo)

        if total == 0:
            raise HTTPException(status_code=400, detail="El archivo llegó vacío.")

        duracion = audio.obtener_duracion_seg(ruta)
        storage_path = storage.subir_archivo_desde_ruta(ruta, nombre_original, carpeta="chat-audios")

        return {
            "storage_path": storage_path,
            "nombre_original": nombre_original,
            "tamano_mb": round(total / (1024 * 1024), 2),
            "duracion_seg": duracion,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/mensajes/{mensaje_id}/audio-url")
def obtener_url_audio_chat(
    mensaje_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    URL firmada para escuchar/descargar el audio adjunto a un mensaje.
    Trae la fila con `usuario.client` (RLS) a propósito: si este
    usuario no puede ver el chat, la consulta no devuelve nada — nunca
    se llega a generar una URL para quien no debería tenerla.
    """
    resp = (
        usuario.client.table("mensajes_chat")
        .select("audio_storage_path")
        .eq("id", mensaje_id)
        .maybe_single()
        .execute()
    )
    fila = resp.data
    if not fila or not fila.get("audio_storage_path"):
        raise HTTPException(status_code=404, detail="Mensaje no encontrado o sin audio adjunto.")

    url = storage.generar_url_descarga(fila["audio_storage_path"], expira_seg=600)
    return {"url": url}


@router.delete("/mensajes")
def limpiar_chat(
    usuario: UsuarioAutenticado = Depends(requiere_admin),
):
    """
    Vacía TODO el historial del chat de una — pensado para después de
    procesar lo que mandaron los suscriptores por acá (ver /chat/audio),
    no para borrar un mensaje puntual (eso ya lo cubre la policy RLS
    "borrar_mensaje_propio_o_admin" directo desde el frontend, sin pasar
    por este backend). Pasa por acá porque además de las filas hay que
    borrar de R2 cada audio adjunto — si no, quedarían huérfanos ahí
    (ocupando espacio en el bucket) sin ninguna fila que los referencie.
    """
    resp = usuario.client.table("mensajes_chat").select("id, audio_storage_path").execute()
    filas = resp.data or []
    if not filas:
        return {"ok": True, "borrados": 0}

    for fila in filas:
        if fila.get("audio_storage_path"):
            try:
                storage.borrar_archivo(fila["audio_storage_path"])
            except Exception:
                # Si el archivo ya no estaba en R2 (o falla transitoriamente),
                # igual queremos vaciar el chat — no bloqueamos por esto.
                logger.exception("No se pudo borrar de R2 el audio del mensaje %s", fila["id"])

    ids = [fila["id"] for fila in filas]
    usuario.client.table("mensajes_chat").delete().in_("id", ids).execute()
    return {"ok": True, "borrados": len(ids)}


@router.post("/mensajes/{mensaje_id}/analizar", response_model=AnalisisTrackResponse)
def analizar_audio_de_chat(
    mensaje_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_admin),
):
    """
    Puente para catalogar directo un audio que alguien ya compartió por
    el chat (ver POST /chat/audio) — sin que el admin tenga que bajarlo
    a su computadora y volver a subirlo a mano. Lo trae de R2 (donde ya
    está, en chat-audios/) a disco acá en el servidor, y de ahí en más
    corre EXACTAMENTE el mismo pipeline que /tracks/analyze
    (transcodifica, detecta BPM/key/fingerprint, chequea duplicados por
    fingerprint, deja el archivo en espera) — ver
    tracks.procesar_y_dejar_en_espera, compartida entre los dos. El
    resultado se confirma después con el mismo /tracks/confirm de
    siempre, con el admin completando título/remixer/género como en
    cualquier otra subida (acá todavía no los tenemos, por eso no se
    chequean duplicados por texto, solo por fingerprint).
    """
    resp = (
        usuario.client.table("mensajes_chat")
        .select("audio_storage_path, audio_nombre_original")
        .eq("id", mensaje_id)
        .maybe_single()
        .execute()
    )
    fila = resp.data
    if not fila or not fila.get("audio_storage_path"):
        raise HTTPException(status_code=404, detail="Mensaje no encontrado o sin audio adjunto.")

    nombre_original = fila.get("audio_nombre_original") or "audio.mp3"
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else "mp3"

    tmp_dir = Path(tempfile.mkdtemp(prefix="clubpack_chatanalyze_"))
    try:
        ruta_subida = tmp_dir / f"original.{extension}"
        try:
            storage.descargar_a_archivo(fila["audio_storage_path"], ruta_subida)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"No se pudo bajar el audio de R2: {e}")

        return procesar_y_dejar_en_espera(ruta_subida, nombre_original, usuario, tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
