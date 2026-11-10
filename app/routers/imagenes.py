"""
Imágenes que no son el audio en sí: portadas de track y las imágenes
de "Herramientas DJ". Van por R2 igual que el audio (misma cuenta,
mismo bucket, prefijos distintos: "portadas/" y "herramientas/"), así
que necesitan este backend para las credenciales — el frontend nunca
las tiene.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import storage
from ..config import settings
from ..deps import UsuarioAutenticado, requiere_acceso_activo, requiere_admin
from ..schemas import ImagenUrlResponse, ImagenSubidaResponse

router = APIRouter(tags=["imagenes"])
logger = logging.getLogger("clubpack.imagenes")

PREFIJOS_PERMITIDOS = ("portadas/", "herramientas/")


def _es_admin(usuario: UsuarioAutenticado) -> bool:
    resp = usuario.client.rpc("es_admin").execute()
    return bool(resp.data)


async def _leer_imagen_validada(file: UploadFile) -> bytes:
    contenido = await file.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="La imagen llegó vacía.")
    tamano_mb = len(contenido) / (1024 * 1024)
    if tamano_mb > settings.MAX_IMAGEN_MB:
        raise HTTPException(
            status_code=413,
            detail=f"La imagen pesa {tamano_mb:.1f}MB, el máximo permitido es {settings.MAX_IMAGEN_MB}MB.",
        )
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="El archivo no es una imagen.")
    return contenido


def _extension_de(file: UploadFile) -> str:
    nombre = file.filename or ""
    if "." in nombre:
        ext = nombre.rsplit(".", 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png", "webp"):
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


@router.get("/imagenes/url", response_model=ImagenUrlResponse)
def url_imagen(
    path: str,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    URL firmada temporal para mostrar una portada o una imagen de
    herramienta DJ (el bucket de R2 es privado). Restringido a los
    prefijos de imágenes para que esto no se pueda usar como una
    puerta genérica de lectura de cualquier key del bucket (p.ej.
    "tracks/..." con el audio).
    """
    if not path.startswith(PREFIJOS_PERMITIDOS):
        raise HTTPException(status_code=403, detail="Esa ruta no se puede leer por acá.")
    url = storage.generar_url_descarga(path, expira_seg=3600)
    return ImagenUrlResponse(url=url)


@router.post("/tracks/{track_id}/portada", response_model=ImagenSubidaResponse)
async def subir_portada(
    track_id: str,
    file: UploadFile,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    Subida manual (o reemplazo) de la portada de un track. La puede
    hacer quien subió el track, o un admin — mismo criterio que para
    editarlo.
    """
    resp = (
        usuario.client.table("tracks")
        .select("id, subido_por, portada_url")
        .eq("id", track_id)
        .maybe_single()
        .execute()
    )
    track = resp.data
    if not track:
        raise HTTPException(status_code=404, detail="Track no encontrado.")
    if track.get("subido_por") != usuario.user_id and not _es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo quien subió el track (o un admin) puede cambiar su portada.")

    contenido = await _leer_imagen_validada(file)
    extension = _extension_de(file)
    key = f"portadas/{track_id}.{extension}"
    storage.subir_bytes(contenido, key)

    usuario.client.table("tracks").update({"portada_url": key}).eq("id", track_id).execute()

    portada_anterior = track.get("portada_url")
    if portada_anterior and portada_anterior != key:
        try:
            storage.borrar_archivo(portada_anterior)
        except Exception:
            logger.exception("No se pudo borrar la portada anterior %s", portada_anterior)

    return ImagenSubidaResponse(path=key)


@router.delete("/tracks/{track_id}/portada")
async def quitar_portada(
    track_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    Saca la portada propia de un track y lo deja con la genérica de
    ClubPack (portada_url en null). Pensado para cuando se subió una
    portada equivocada (p.ej. quedó pegada la de otro tema al cargar)
    y lo que se quiere es volver al estado "sin portada propia", no
    reemplazarla por otra. Mismo criterio de permisos que subir_portada.
    """
    resp = (
        usuario.client.table("tracks")
        .select("id, subido_por, portada_url")
        .eq("id", track_id)
        .maybe_single()
        .execute()
    )
    track = resp.data
    if not track:
        raise HTTPException(status_code=404, detail="Track no encontrado.")
    if track.get("subido_por") != usuario.user_id and not _es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo quien subió el track (o un admin) puede cambiar su portada.")

    portada_actual = track.get("portada_url")
    if not portada_actual:
        return {"ok": True}

    usuario.client.table("tracks").update({"portada_url": None}).eq("id", track_id).execute()

    try:
        storage.borrar_archivo(portada_actual)
    except Exception:
        logger.exception("No se pudo borrar el archivo de portada %s", portada_actual)

    return {"ok": True}


@router.post("/admin/herramientas/imagen", response_model=ImagenSubidaResponse)
async def subir_imagen_herramienta(
    file: UploadFile,
    usuario: UsuarioAutenticado = Depends(requiere_admin),
):
    """
    Sube la imagen de una Herramienta DJ a R2 y devuelve su key. El
    admin la usa ANTES de crear/editar la fila en herramientas_dj
    (esa sí la hace directo contra Supabase desde el frontend — acá
    solo se necesita pasar por el backend por las credenciales de R2).
    """
    contenido = await _leer_imagen_validada(file)
    extension = _extension_de(file)
    key = f"herramientas/{uuid.uuid4()}.{extension}"
    storage.subir_bytes(contenido, key)
    return ImagenSubidaResponse(path=key)
