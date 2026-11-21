"""
Nombres de archivo para descargas de uno o varios tracks (selecciones
del catálogo o playlists enteras).

Hasta hace poco este módulo también armaba el ZIP en sí (ver
armar_zip, ya eliminada): bajaba cada archivo de R2 al servidor y lo
volvía a subir como ZIP al navegador, el doble de transferencia que
una descarga individual — y esos bytes se quedaban en la memoria del
proceso mientras tanto. Con el plan gratis de Render (512MB) varias
selecciones grandes pedidas en simultáneo podían sumar más memoria de
la que hay disponible (mismo tipo de problema que las subidas de
archivos largos, ver app/audio.py::precalentar_librosa). Ahora
tracks.py y playlists.py devuelven una URL firmada por track en vez de
armar nada acá — ver DescargaMultipleResponse en app/schemas.py — así
que esto quedó reducido a los helpers de nombre de archivo.
"""

import re


def _nombre_seguro(texto: str) -> str:
    limpio = re.sub(r'[\\/*?:"<>|]', "", texto).strip()
    return limpio[:120] or "track"


def nombre_descarga(track: dict) -> str:
    """
    Arma el nombre de archivo (sin extensión) para una descarga, igual
    para una pista suelta, un ZIP de selección o un ZIP de playlist:

        "Artista - Pista - Remixer"   (el remixer solo si lo tiene)

    A pedido explícito de Edgar, el BPM quedó afuera del nombre del
    archivo físico (antes iba adelante: "BPM Artista - Pista -
    Remixer") — pero el dato en sí no se toca para nada: sigue
    analizándose, guardándose y mostrándose en la plataforma igual que
    siempre (ver audio.detectar_bpm y la columna tracks.bpm). Esta
    función solo arma el nombre para la descarga, así que alcanza con
    no incluirlo acá.

    `track` necesita al menos 'titulo'; 'artista_original' y
    'remixer' son opcionales (pueden faltar o venir None).
    """
    artista = (track.get("artista_original") or "").strip()
    titulo = (track.get("titulo") or "").strip()
    remixer = (track.get("remixer") or "").strip()

    piezas = [p for p in (artista, titulo, remixer) if p]
    return _nombre_seguro(" - ".join(piezas))
