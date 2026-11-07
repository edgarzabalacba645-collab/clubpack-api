import base64
import logging
import shutil
import tempfile
from pathlib import Path

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

# Se escribe el upload a disco en pedazos de este tamaño — nunca se junta
# el archivo entero (sesiones largas ≈200MB) en un solo bytes de Python.
# Ver la nota de memoria al principio de app/audio.py.
CHUNK_SIZE = 1024 * 1024  # 1MB


def _registrar_descarga(usuario: UsuarioAutenticado, track_id: str):
    try:
        usuario.client.table("descargas").insert(
            {"usuario_id": usuario.user_id, "track_id": track_id}
        ).execute()
    except Exception:
        # No queremos que falle la descarga en sí por un problema al loguearla.
        logger.exception("No se pudo registrar la descarga del track %s", track_id)


@router.post("/analyze", response_model=AnalisisTrackResponse)
def analizar_track(
    file: UploadFile,
    titulo: str = Form(...),
    remixer: str = Form(""),
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    Paso 1 de la subida: transcodifica a MP3 320, detecta duración/BPM/
    fingerprint, chequea duplicados por texto, y deja el archivo listo
    en espera de confirmación (ver /tracks/confirm).

    A propósito es un `def` normal (no `async def`): FastAPI corre los
    endpoints sync en un thread pool solo, así el trabajo pesado de acá
    (ffmpeg, librosa, fpcalc — todo bloqueante) no congela el event loop
    para el resto de los DJs mientras alguien sube una sesión larga.

    Y a propósito el archivo NUNCA se junta entero como bytes de Python
    (ver nota de memoria en app/audio.py) — se escribe a disco en pedazos
    de a `CHUNK_SIZE`, y de ahí en más todo el pipeline (transcodificar,
    analizar, dejar en espera) trabaja sobre esa ruta en disco.
    """
    nombre_original = file.filename or "audio.mp3"
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else "bin"
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024

    tmp_dir = Path(tempfile.mkdtemp(prefix="clubpack_analyze_"))
    try:
        ruta_subida = tmp_dir / f"original.{extension}"
        total = 0
        with open(ruta_subida, "wb") as destino:
            while True:
                pedazo = file.file.read(CHUNK_SIZE)
                if not pedazo:
                    break
                total += len(pedazo)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"El archivo supera el máximo permitido de {settings.MAX_UPLOAD_MB}MB.",
                    )
                destino.write(pedazo)

        if total == 0:
            raise HTTPException(status_code=400, detail="El archivo llegó vacío.")

        advertencias: list[str] = []
        try:
            ruta_mp3, aviso = audio.transcodificar_a_mp3_320(ruta_subida, tmp_dir / "convertido.mp3")
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if aviso:
            advertencias.append(aviso)
        # Ya no hace falta el archivo crudo subido una vez transcodificado
        # (si ffmpeg no estaba disponible, ruta_mp3 ES ruta_subida — no
        # hay nada que borrar en ese caso).
        if ruta_mp3 != ruta_subida:
            ruta_subida.unlink(missing_ok=True)

        analisis = audio.analizar_track(ruta_mp3)

        # Fingerprint primero (es el mismo audio posta, no solo texto parecido)
        # y de ahí se completa con los parecidos por texto, sin repetir ids.
        dup_fingerprint = duplicados.chequear_fingerprint_exacto(usuario.client, analisis["fingerprint"])
        dup_texto = duplicados.chequear_antes_de_subir(usuario.client, titulo, remixer)
        vistos = {d["id"] for d in dup_fingerprint}
        duplicados_sospechosos = dup_fingerprint + [d for d in dup_texto if d["id"] not in vistos]

        # Solo lee tags ID3, no vuelve a transcodificar. Si el archivo trae
        # carátula embebida, se manda como preview en base64 — sin poder
        # verla, es muy difícil decidir si conviene mantenerla o usar la de
        # ClubPack (el usuario tendría que ir a buscarla por otro lado y
        # volver). Esto va ANTES de staging.guardar porque ese paso MUEVE
        # el archivo de acá — después de eso ya no está en esta ruta.
        tiene_caratula = False
        caratula_preview: str | None = None
        try:
            caratula = audio.extraer_caratula(ruta_mp3)
            if caratula:
                imagen_bytes, extension_caratula = caratula
                tiene_caratula = True
                # La elección de mantenerla o no se ofrece igual aunque sea
                # pesada — lo único que se omite es la miniatura en sí (en
                # base64 pesa ~33% más que el original).
                if len(imagen_bytes) <= MAX_CARATULA_PREVIEW_BYTES:
                    mime = "image/png" if extension_caratula == "png" else "image/jpeg"
                    caratula_preview = f"data:{mime};base64,{base64.b64encode(imagen_bytes).decode('ascii')}"
        except Exception:
            tiene_caratula = False
            caratula_preview = None

        tamano_mb = round(ruta_mp3.stat().st_size / (1024 * 1024), 2)
        staging_id = staging.guardar(ruta_mp3, nombre_original, usuario.user_id)

        return AnalisisTrackResponse(
            staging_id=staging_id,
            nombre_original=nombre_original,
            tamano_mb=tamano_mb,
            duracion_seg=analisis["duracion_seg"],
            bpm_detectado=analisis["bpm_detectado"],
            key_detectada=analisis["key_detectada"],
            fingerprint=analisis["fingerprint"],
            advertencias=advertencias,
            duplicados_sospechosos=[DuplicadoCandidato(**d) for d in duplicados_sospechosos],
            tiene_caratula_original=tiene_caratula,
            caratula_original_preview=caratula_preview,
        )
    finally:
        # Lo que haya quedado acá (el original si se descartó con error,
        # restos si algo falló a mitad de camino) — el convertido ya se
        # movió afuera por staging.guardar en el camino feliz.
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/confirm", response_model=TrackOut)
def confirmar_subida(
    body: ConfirmarSubidaRequest,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """Paso 2: sube a R2 el archivo ya analizado y crea el track en el catálogo."""
    entrada = staging.retirar(body.staging_id, usuario.user_id)
    if entrada is None:
        # El "30 min" venía HARDCODEADO acá como texto — quedó mintiendo
        # cuando STAGING_TTL_SEG se estiró a 90 min (ver app/config.py):
        # el vencimiento real ya era de 90 min, pero el mensaje seguía
        # diciendo 30. Ahora sale del mismo valor que usa app/staging.py,
        # para que no vuelvan a desincronizarse.
        minutos = settings.STAGING_TTL_SEG // 60
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró el análisis — puede haber vencido ({minutos} min). Volvé a subir el archivo.",
        )

    # Todo lo de acá trabaja sobre entrada.ruta_mp3 (el archivo en disco),
    # nunca sobre sus bytes completos en memoria de Python — con sesiones
    # largas (~200MB) esa carga completa era justo lo que podía hacer caer
    # el proceso en el plan gratuito de Render (ver nota en app/audio.py).
    # El archivo temporal recién se borra al final, en el finally, una vez
    # que ya no hace falta para nada más (ni siquiera para la carátula).
    try:
        storage_path = storage.subir_archivo_desde_ruta(entrada.ruta_mp3, entrada.nombre_original)

        fingerprint = audio.calcular_fingerprint(entrada.ruta_mp3)
        duracion_seg = audio.obtener_duracion_seg(entrada.ruta_mp3)
        bpm_final = (
            body.bpm
            if body.bpm is not None
            else audio.detectar_bpm(entrada.ruta_mp3, duracion_seg=duracion_seg)
        )
        key_final = (
            body.key_armonica
            if body.key_armonica is not None
            else audio.detectar_key(entrada.ruta_mp3, duracion_seg=duracion_seg)
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
            "tamano_mb": round(entrada.ruta_mp3.stat().st_size / (1024 * 1024), 2),
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
                caratula = audio.extraer_caratula(entrada.ruta_mp3)
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
    finally:
        staging.borrar(entrada)


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
