"""
Arma un ZIP en memoria con varios tracks para descargar de una,
pensado para selecciones del catálogo o playlists enteras.

Portado de utils/zip_download.py del prototipo en Streamlit — misma lógica.

Nota de costos: esto baja cada archivo de R2 al servidor y lo vuelve a
subir como ZIP al navegador del usuario — el doble de transferencia que
una descarga individual. Para 20-30 usuarios con MP3 320kbps no es un
problema, pero si el grupo crece mucho conviene revisarlo.
"""

import io
import re
import zipfile

from .storage import descargar_bytes


def _nombre_seguro(texto: str) -> str:
    limpio = re.sub(r'[\\/*?:"<>|]', "", texto).strip()
    return limpio[:120] or "track"


def nombre_descarga(track: dict) -> str:
    """
    Arma el nombre de archivo (sin extensión) para una descarga, igual
    para una pista suelta, un ZIP de selección o un ZIP de playlist:

        "BPM Artista - Pista - Remixer"   (el remixer solo si lo tiene,
                                            el BPM solo si está cargado)

    `track` necesita al menos 'titulo'; 'bpm', 'artista_original' y
    'remixer' son opcionales (pueden faltar o venir None).
    """
    partes_izquierda = []
    bpm = track.get("bpm")
    if bpm is not None:
        partes_izquierda.append(str(round(bpm)))
    artista = (track.get("artista_original") or "").strip()
    if artista:
        partes_izquierda.append(artista)
    izquierda = " ".join(partes_izquierda)

    titulo = (track.get("titulo") or "").strip()
    remixer = (track.get("remixer") or "").strip()

    piezas = [p for p in (izquierda, titulo, remixer) if p]
    return _nombre_seguro(" - ".join(piezas))


def armar_zip(tracks: list[dict]) -> bytes:
    """
    tracks: lista de dicts con al menos 'titulo' y 'storage_path' (ver
    nombre_descarga para los demás campos que usa el nombre de archivo).
    Devuelve los bytes del ZIP.
    """
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        nombres_usados = set()
        for t in tracks:
            contenido = descargar_bytes(t["storage_path"])

            base = nombre_descarga(t)
            nombre_archivo = f"{base}.mp3"
            sufijo = 2
            while nombre_archivo in nombres_usados:
                nombre_archivo = f"{base} ({sufijo}).mp3"
                sufijo += 1
            nombres_usados.add(nombre_archivo)

            zf.writestr(nombre_archivo, contenido)

    buffer.seek(0)
    return buffer.read()
