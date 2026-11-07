from fastapi import APIRouter, Depends, HTTPException

from .. import storage, zip_utils
from ..deps import UsuarioAutenticado, requiere_acceso_activo
from ..schemas import ArchivoDescarga, DescargaMultipleResponse
from .tracks import _registrar_descarga

router = APIRouter(prefix="/playlists", tags=["playlists"])


@router.post("/{playlist_id}/zip", response_model=DescargaMultipleResponse)
def descargar_playlist_zip(
    playlist_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
    """
    Ver el comentario de tracks.py::descargar_zip — misma idea acá: en vez
    de armar un ZIP de la playlist entera en memoria, devuelve una URL
    firmada por track para que el navegador las baje directo de R2.
    """
    playlist_resp = (
        usuario.client.table("playlists")
        .select("id, nombre")
        .eq("id", playlist_id)
        .maybe_single()
        .execute()
    )
    playlist = playlist_resp.data
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist no encontrada.")

    filas_resp = (
        usuario.client.table("playlist_tracks")
        .select("orden, tracks(id, titulo, artista_original, remixer, bpm, storage_path)")
        .eq("playlist_id", playlist_id)
        .order("orden")
        .execute()
    )
    tracks = [f["tracks"] for f in (filas_resp.data or []) if f.get("tracks")]
    if not tracks:
        raise HTTPException(status_code=404, detail="La playlist no tiene tracks.")

    expira = 300
    archivos: list[ArchivoDescarga] = []
    nombres_usados: set[str] = set()
    for t in tracks:
        base = zip_utils.nombre_descarga(t)
        nombre = f"{base}.mp3"
        sufijo = 2
        while nombre in nombres_usados:
            nombre = f"{base} ({sufijo}).mp3"
            sufijo += 1
        nombres_usados.add(nombre)

        url = storage.generar_url_descarga(t["storage_path"], nombre_descarga=nombre, expira_seg=expira)
        archivos.append(ArchivoDescarga(nombre=nombre, url=url))
        _registrar_descarga(usuario, t["id"])

    return DescargaMultipleResponse(archivos=archivos, expira_seg=expira)
