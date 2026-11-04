import logging

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from .. import audio, duplicados, staging, storage, zip_utils
from ..config import settings
from ..deps import UsuarioAutenticado, requiere_acceso_activo, requiere_admin
from ..schemas import (
    AnalisisTrackResponse,
    ConfirmarSubidaRequest,
    DescargaUrlResponse,
    DuplicadoCandidato,
    TrackOut,
    ZipRequest,
)

router = APIRouter(prefix="/tracks", tags=["tracks"])
logger = logging.getLogger("clubpack.tracks")


def _registrar_descarga(usuario: UsuarioAutenticado, track_id: str):
    try:
        usuario.client.table("descargas").insert(
            {"usuario_id": usuario.user_id, "track_id": track_id}
        ).execute()
    except Exception:
        # No queremos que falle la descarga en sí por un problema al loguearla.
        logger.exception("No se pudo registrar la descarga del track %s", track_id)


@router.post("/analyze", response_model=AnalisisTrackResponse)
async def analizar_track(
    file: UploadFile,
    titulo: str = Form(...),
    remixer: str = Form(""),
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    Paso 1 de la subida: transcodifica a MP3 320, detecta duración/BPM/
    fingerprint, chequea duplicados por texto, y deja el archivo listo
    en espera de confirmación (ver /tracks/confirm).
    """
    contenido = await file.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío.")

    tamano_mb = len(contenido) / (1024 * 1024)
    if tamano_mb > settings.MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=413,
            detail=f"El archivo pesa {tamano_mb:.1f}MB, el máximo permitido es {settings.MAX_UPLOAD_MB}MB.",
        )

    advertencias: list[str] = []
    try:
        mp3_bytes, aviso = audio.transcodificar_a_mp3_320(contenido, file.filename or "audio")
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if aviso:
        advertencias.append(aviso)

    analisis = audio.analizar_track(mp3_bytes)
    duplicados_sospechosos = duplicados.chequear_antes_de_subir(usuario.client, titulo, remixer)

    staging_id = staging.guardar(mp3_bytes, file.filename or "audio.mp3", usuario.user_id)

    return AnalisisTrackResponse(
        staging_id=staging_id,
        nombre_original=file.filename or "audio.mp3",
        tamano_mb=round(len(mp3_bytes) / (1024 * 1024), 2),
        duracion_seg=analisis["duracion_seg"],
        bpm_detectado=analisis["bpm_detectado"],
        key_detectada=analisis["key_detectada"],
        fingerprint=analisis["fingerprint"],
        advertencias=advertencias,
        duplicados_sospechosos=[DuplicadoCandidato(**d) for d in duplicados_sospechosos],
    )


@router.post("/confirm", response_model=TrackOut)
def confirmar_subida(
    body: ConfirmarSubidaRequest,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """Paso 2: sube a R2 el archivo ya analizado y crea el track en el catálogo."""
    entrada = staging.retirar(body.staging_id, usuario.user_id)
    if entrada is None:
        raise HTTPException(
            status_code=404,
            detail="No se encontró el análisis — puede haber vencido (30 min). Volvé a subir el archivo.",
        )

    storage_path = storage.subir_archivo(entrada.contenido_mp3, entrada.nombre_original)

    fingerprint = audio.calcular_fingerprint(entrada.contenido_mp3)
    duracion_seg = audio.obtener_duracion_seg(entrada.contenido_mp3)
    bpm_final = (
        body.bpm
        if body.bpm is not None
        else audio.detectar_bpm(entrada.contenido_mp3, duracion_seg=duracion_seg)
    )
    key_final = (
        body.key_armonica
        if body.key_armonica is not None
        else audio.detectar_key(entrada.contenido_mp3, duracion_seg=duracion_seg)
    )

    fila = {
        "titulo": body.titulo,
        "artista_original": body.artista_original,
        "remixer": body.remixer,
        "genero_id": body.genero_id,
        "bpm": bpm_final,
        "key_armonica": key_final,
        "tags": body.tags,
        "storage_path": storage_path,
        "duracion_seg": duracion_seg,
        "tamano_mb": round(len(entrada.contenido_mp3) / (1024 * 1024), 2),
        "subido_por": usuario.user_id,
        "audio_fingerprint": fingerprint,
    }

    try:
        resp = usuario.client.table("tracks").insert(fila).execute()
    except Exception as e:
        # Si falla el insert, no dejamos el archivo huérfano en R2.
        storage.borrar_archivo(storage_path)
        raise HTTPException(status_code=400, detail=f"No se pudo crear el track: {e}")

    if not resp.data:
        storage.borrar_archivo(storage_path)
        raise HTTPException(status_code=400, detail="No se pudo crear el track.")

    return TrackOut(**resp.data[0])


@router.get("/{track_id}/download-url", response_model=DescargaUrlResponse)
def obtener_url_descarga(
    track_id: str,
    registrar: bool = True,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    URL firmada para bajar (o previsualizar) un track.
    `registrar=false` la pide el player de preview del catálogo, para no
    contar una escucha como una descarga en el Top 50. El botón de
    descarga de verdad deja `registrar` en True (default).
    """
    resp = (
        usuario.client.table("tracks")
        .select("id, titulo, artista_original, remixer, bpm, storage_path")
        .eq("id", track_id)
        .maybe_single()
        .execute()
    )
    track = resp.data
    if not track:
        raise HTTPException(status_code=404, detail="Track no encontrado.")

    nombre = f"{zip_utils.nombre_descarga(track)}.mp3"
    expira = 300
    url = storage.generar_url_descarga(track["storage_path"], nombre_descarga=nombre, expira_seg=expira)

    if registrar:
        _registrar_descarga(usuario, track_id)

    return DescargaUrlResponse(url=url, expira_seg=expira)


@router.post("/zip")
def descargar_zip(
    body: ZipRequest,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    resp = (
        usuario.client.table("tracks")
        .select("id, titulo, artista_original, remixer, bpm, storage_path")
        .in_("id", body.track_ids)
        .execute()
    )
    tracks = resp.data or []
    if not tracks:
        raise HTTPException(status_code=404, detail="No se encontró ninguno de esos tracks.")

    contenido_zip = zip_utils.armar_zip(tracks)

    for t in tracks:
        _registrar_descarga(usuario, t["id"])

    nombre_zip = body.nombre_zip or "clubpack"
    nombre_zip = zip_utils._nombre_seguro(nombre_zip)

    return StreamingResponse(
        iter([contenido_zip]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nombre_zip}.zip"'},
    )


@router.delete("/{track_id}")
def borrar_track(
    track_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_admin),
):
    """
    Borrado de un track desde Admin (p.ej. al resolver un duplicado).
    Pasa por acá y no directo contra Supabase porque borrar el archivo de
    R2 necesita las credenciales del bucket, que el frontend nunca tiene.
    Deja el registro en la tabla (estado='eliminado') en vez de un delete
    duro, para no perder el historial de descargas/duplicados asociado.
    """
    resp = (
        usuario.client.table("tracks")
        .select("id, storage_path")
        .eq("id", track_id)
        .maybe_single()
        .execute()
    )
    track = resp.data
    if not track:
        raise HTTPException(status_code=404, detail="Track no encontrado.")

    try:
        storage.borrar_archivo(track["storage_path"])
    except Exception:
        # Si el archivo ya no estaba en R2 (o el bucket falla transitoriamente),
        # igual queremos sacar el track del catálogo — no bloqueamos por esto.
        logger.exception("No se pudo borrar de R2 el archivo del track %s", track_id)

    usuario.client.table("tracks").update({"estado": "eliminado"}).eq("id", track_id).execute()
    return {"ok": True}
