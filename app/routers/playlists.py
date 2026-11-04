from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import zip_utils
from ..deps import UsuarioAutenticado, requiere_acceso_activo
from .tracks import _registrar_descarga

router = APIRouter(prefix="/playlists", tags=["playlists"])


@router.post("/{playlist_id}/zip")
def descargar_playlist_zip(
    playlist_id: str,
    usuario: UsuarioAutenticado = Depends(requiere_acceso_activo),
):
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

    contenido_zip = zip_utils.armar_zip(tracks)

    for t in tracks:
        _registrar_descarga(usuario, t["id"])

    nombre_zip = zip_utils._nombre_seguro(playlist["nombre"])

    return StreamingResponse(
        iter([contenido_zip]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nombre_zip}.zip"'},
    )
