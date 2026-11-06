import base64
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from .. import audio, duplicados, staging, storage, zip_utils
from ..config import settings
from ..deps import UsuarioAutenticado, get_usuario_actual, requiere_acceso_activo, requiere_admin
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

# Tope para mandar la carátula embebida como miniatura (base64) en la
# respuesta de /analyze. Por arriba de esto, igual se informa que el
# archivo SÍ tiene carátula (tiene_caratula_original) para que el usuario
# pueda elegir mantenerla o no — simplemente no se manda la miniatura, para
# no inflar la respuesta (y la memoria del request) con una imagen pesada.
MAX_CARATULA_PREVIEW_BYTES = 800 * 1024  # 800KB


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
    # Ya no hace falta el audio crudo subido — lo soltamos ahora en vez de
    # esperar al final de la función, para no tener la copia cruda Y la
    # transcodificada en memoria al mismo tiempo más de lo necesario.
    del contenido
    if aviso:
        advertencias.append(aviso)

    analisis = audio.analizar_track(mp3_bytes)

    # Fingerprint primero (es el mismo audio posta, no solo texto parecido)
    # y de ahí se completa con los parecidos por texto, sin repetir ids.
    dup_fingerprint = duplicados.chequear_fingerprint_exacto(usuario.client, analisis["fingerprint"])
    dup_texto = duplicados.chequear_antes_de_subir(usuario.client, titulo, remixer)
    vistos = {d["id"] for d in dup_fingerprint}
    duplicados_sospechosos = dup_fingerprint + [d for d in dup_texto if d["id"] not in vistos]

    staging_id = staging.guardar(mp3_bytes, file.filename or "audio.mp3", usuario.user_id)

    # Solo lee tags ID3, no vuelve a transcodificar. Si el archivo trae
    # carátula embebida, se manda como preview en base64 — sin poder verla,
    # es muy difícil decidir si conviene mantenerla o usar la de ClubPack
    # (el usuario tendría que ir a buscarla por otro lado y volver).
    tiene_caratula = False
    caratula_preview: str | None = None
    try:
        caratula = audio.extraer_caratula(mp3_bytes)
        if caratula:
            imagen_bytes, extension = caratula
            tiene_caratula = True
            # La elección de mantenerla o no se ofrece igual aunque sea
            # pesada — lo único que se omite es la miniatura en sí (en
            # base64 pesa ~33% más que el original).
            if len(imagen_bytes) <= MAX_CARATULA_PREVIEW_BYTES:
                mime = "image/png" if extension == "png" else "image/jpeg"
                caratula_preview = f"data:{mime};base64,{base64.b64encode(imagen_bytes).decode('ascii')}"
    except Exception:
        tiene_caratula = False
        caratula_preview = None

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
        tiene_caratula_original=tiene_caratula,
        caratula_original_preview=caratula_preview,
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

    # Se lee una sola vez a memoria (en vez de la propiedad `contenido_mp3`,
    # que relee el archivo de disco cada vez) y se borra el archivo
    # temporal apenas se termina de leer — no hace falta esperar a que
    # venza el TTL de espera si ya se confirmó la subida.
    try:
        contenido_mp3 = entrada.contenido_mp3
    finally:
        staging.borrar(entrada)

    storage_path = storage.subir_archivo(contenido_mp3, entrada.nombre_original)

    fingerprint = audio.calcular_fingerprint(contenido_mp3)
    duracion_seg = audio.obtener_duracion_seg(contenido_mp3)
    bpm_final = (
        body.bpm
        if body.bpm is not None
        else audio.detectar_bpm(contenido_mp3, duracion_seg=duracion_seg)
    )
    key_final = (
        body.key_armonica
        if body.key_armonica is not None
        else audio.detectar_key(contenido_mp3, duracion_seg=duracion_seg)
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
        "tamano_mb": round(len(contenido_mp3) / (1024 * 1024), 2),
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

    track_creado = resp.data[0]

    # Si el archivo original traía una carátula embebida Y el usuario
    # eligió mantenerla (ver tiene_caratula_original/usar_caratula_original
    # en schemas.py), se guarda sola como portada. Si no, queda sin
    # portada propia y el catálogo muestra la genérica de ClubPack — a
    # veces la carátula embebida es de otro servicio pago y no queda bien,
    # por eso esto es una elección y no algo automático. Si algo falla acá
    # no se cae la subida: el track ya quedó creado, la portada se puede
    # agregar después a mano.
    if body.usar_caratula_original:
        try:
            caratula = audio.extraer_caratula(contenido_mp3)
            if caratula:
                imagen_bytes, extension = caratula
                key_portada = f"portadas/{track_creado['id']}.{extension}"
                storage.subir_bytes(imagen_bytes, key_portada)
                usuario.client.table("tracks").update({"portada_url": key_portada}).eq(
                    "id", track_creado["id"]
                ).execute()
                track_creado["portada_url"] = key_portada
        except Exception:
            logger.exception("No se pudo extraer/guardar la carátula embebida del track %s", track_creado["id"])

    return TrackOut(**track_creado)


@router.get("/{track_id}/download-url", response_model=DescargaUrlResponse)
def obtener_url_descarga(
    track_id: str,
    registrar: bool = True,
    usuario: UsuarioAutenticado = Depends(get_usuario_actual),
):
    """
    URL firmada para bajar (o previsualizar) un track.
    `registrar=false` la pide el player de preview del catálogo, para no
    contar una escucha como una descarga en el Top 50. El botón de
    descarga de verdad deja `registrar` en True (default).

    A propósito esto YA NO exige `requiere_acceso_activo` de entrada: los
    tracks de género "de prueba" (Cuarteto/Cachengue — ver policy
    "ver_tracks_con_acceso" en sql/006_catalogo_freemium.sql) son visibles
    para cualquier usuario logueado sin suscripción, así los DJs nuevos
    pueden escuchar antes de sumarse. El SELECT de abajo ya respeta eso
    por RLS — si no devuelve nada, este usuario no tiene permiso para ver
    (ni por lo tanto escuchar) este track puntual. La descarga real
    (`registrar=true`) sí exige suscripción activa siempre, sea cual sea
    el género — por eso ese chequeo queda explícito acá, aparte de RLS.
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

    if registrar:
        acceso = usuario.client.rpc("tiene_acceso_activo").execute()
        if not acceso.data:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Necesitás una suscripción activa para descargar. "
                    "Mientras tanto podés escuchar gratis los temas de Cuarteto y Cachengue."
                ),
            )

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
