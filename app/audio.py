"""
Procesamiento de audio: transcodificación a MP3 320kbps, duración,
detección de BPM y fingerprint para duplicados.

Portado de utils/audio.py del prototipo en Streamlit — misma lógica,
sin las llamadas a `st.*` (acá las advertencias se devuelven como datos
para que el endpoint decida cómo mostrarlas).

Requiere en el sistema:
  - ffmpeg / ffprobe   → transcodificación y duración
  - fpcalc (paquete "chromaprint" / "libchromaprint-tools")
                       → fingerprint de audio para detectar duplicados
Si falta alguna, la función correspondiente degrada con gracia
(devuelve None o el archivo sin convertir) en vez de romper el flujo.
"""

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("clubpack.audio")


def _hay_herramienta(nombre: str) -> bool:
    return shutil.which(nombre) is not None


@contextlib.contextmanager
def _archivo_temporal(contenido: bytes, sufijo: str = ".mp3"):
    """
    Escribe `contenido` a un archivo temporal y da su ruta ya CERRADA.

    En Windows, un proceso externo (ffmpeg/ffprobe/fpcalc) no puede abrir
    por nombre un archivo que este proceso todavía tiene abierto — a
    diferencia de Linux/Mac, donde sí se puede sin problema. Por eso acá
    se escribe con `delete=False`, se cierra el handle, y recién ahí se
    da la ruta; se borra solo al salir del `with`.
    """
    f = tempfile.NamedTemporaryFile(suffix=sufijo, delete=False)
    try:
        f.write(contenido)
        f.close()
        yield f.name
    finally:
        try:
            os.unlink(f.name)
        except OSError:
            pass


def transcodificar_a_mp3_320(contenido: bytes, nombre_original: str) -> tuple[bytes, str | None]:
    """
    Convierte cualquier audio soportado por ffmpeg a MP3 CBR 320kbps.
    Devuelve (bytes_resultantes, advertencia_o_None).
    Si ffmpeg no está disponible, devuelve el contenido sin tocar y una
    advertencia (para no romper el flujo en un entorno sin ffmpeg).
    """
    if not _hay_herramienta("ffmpeg"):
        aviso = "ffmpeg no está instalado en este servidor — se subió el archivo sin convertir a MP3 320."
        logger.warning(aviso)
        return contenido, aviso

    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else "bin"

    with tempfile.TemporaryDirectory() as tmp:
        entrada = Path(tmp) / f"original.{extension}"
        salida = Path(tmp) / "salida.mp3"
        entrada.write_bytes(contenido)

        resultado = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(entrada),
                "-codec:a", "libmp3lame", "-b:a", "320k",
                # "-codec:v copy" conserva la carátula embebida (si el
                # archivo original trae una) como "attached_pic" en el
                # MP3 de salida, para poder extraerla después con
                # extraer_caratula(). Si no hay stream de video, ffmpeg
                # simplemente ignora el flag — no rompe nada.
                "-codec:v", "copy", "-id3v2_version", "3",
                str(salida),
            ],
            capture_output=True,
        )
        if resultado.returncode != 0 or not salida.exists():
            raise RuntimeError(
                f"No se pudo convertir el audio a MP3 320kbps: {resultado.stderr.decode(errors='ignore')[-500:]}"
            )
        return salida.read_bytes(), None


def obtener_duracion_seg(contenido_mp3: bytes) -> int | None:
    """Duración en segundos usando ffprobe. None si no se puede calcular."""
    if not _hay_herramienta("ffprobe"):
        return None

    with _archivo_temporal(contenido_mp3) as ruta:
        resultado = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", ruta,
            ],
            capture_output=True,
        )
        if resultado.returncode != 0:
            return None
        try:
            data = json.loads(resultado.stdout)
            return round(float(data["format"]["duration"]))
        except (KeyError, ValueError, json.JSONDecodeError):
            return None


BPM_SEGMENTO_SEG = 25  # cuánto audio se analiza en cada uno de los 3 puntos
BPM_TOLERANCIA = 3.0  # diferencia máxima entre dos lecturas para contarlas como "la misma"
BPM_DURACION_MINIMA_PARA_3_PUNTOS = 15  # por debajo de esto, un solo punto alcanza


def _extraer_segmento_wav(ruta_mp3: str, offset_seg: float, duracion_seg: float) -> bytes | None:
    """
    Extrae un segmento de audio y lo devuelve ya decodificado como WAV
    (PCM), usando ffmpeg directo en vez de dejar que librosa/soundfile
    decodifiquen el MP3. En Windows, el build de libsndfile que usa
    `soundfile` es poco confiable con MP3 real (tira LibsndfileError
    "System error" incluso con archivos válidos); WAV en cambio no tiene
    ninguna ambigüedad de formato y siempre se puede leer.
    """
    resultado = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(max(offset_seg, 0.0)), "-t", str(duracion_seg),
            "-i", ruta_mp3,
            "-ar", "22050", "-ac", "1", "-f", "wav", "pipe:1",
        ],
        capture_output=True,
    )
    if resultado.returncode != 0 or not resultado.stdout:
        logger.warning(
            "ffmpeg no pudo extraer el segmento en offset %.1fs: %s",
            offset_seg, resultado.stderr.decode(errors="ignore")[-300:],
        )
        return None
    return resultado.stdout


def _tempo_en(ruta: str, offset_seg: float) -> float | None:
    """Una sola lectura de tempo, arrancando en offset_seg."""
    import librosa

    try:
        wav_bytes = _extraer_segmento_wav(ruta, offset_seg, BPM_SEGMENTO_SEG)
        if not wav_bytes:
            return None
        y, sr = librosa.load(io.BytesIO(wav_bytes), sr=22050, mono=True)
        if y.size == 0:
            return None
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
        return round(bpm, 1)
    except Exception:
        logger.exception("Falló la detección de BPM en offset %.1fs", offset_seg)
        return None


def detectar_bpm(contenido_mp3: bytes, duracion_seg: int | None = None) -> float | None:
    """
    Estima el BPM tomando 3 lecturas — a los 25%, 50% y 75% de la duración
    del tema — y se queda con la que se repite entre las tres. Una sola
    lectura (sobre todo si es solo del intro) es propensa al "error de
    octava": confundir el doble o la mitad del tempo real, algo muy común
    en géneros con acento marcado en el contratiempo (cuarteto, por
    ejemplo, suele leerse a la mitad). Si las tres lecturas no coinciden,
    devuelve la mediana como mejor estimación disponible.

    `duracion_seg` es opcional — si ya se calculó antes (ver analizar_track),
    pasarlo evita correr ffprobe dos veces. Devuelve None si librosa o
    ffmpeg no están disponibles, o no se pudo leer nada del archivo.
    """
    try:
        import librosa  # noqa: F401  (solo para el ImportError temprano)
    except ImportError:
        return None

    if not _hay_herramienta("ffmpeg"):
        return None

    with _archivo_temporal(contenido_mp3) as ruta:
        total = duracion_seg if duracion_seg is not None else obtener_duracion_seg(contenido_mp3)
        if not total or total < BPM_DURACION_MINIMA_PARA_3_PUNTOS:
            puntos = [0.0]
        else:
            puntos = [total * 0.25, total * 0.50, total * 0.75]

        lecturas = [b for b in (_tempo_en(ruta, p) for p in puntos) if b is not None]

        if not lecturas:
            return None
        if len(lecturas) == 1:
            return lecturas[0]

        return _mejor_lectura(lecturas)


def _mejor_lectura(lecturas: list[float]) -> float:
    grupos: list[list[float]] = []
    for valor in lecturas:
        grupo = next((g for g in grupos if abs(g[0] - valor) <= BPM_TOLERANCIA), None)
        if grupo is not None:
            grupo.append(valor)
        else:
            grupos.append([valor])

    mas_repetido = max(grupos, key=len)
    if len(mas_repetido) >= 2:
        return round(sum(mas_repetido) / len(mas_repetido), 1)

    # Las 3 lecturas difieren entre sí (no hay mayoría): la mediana es la
    # mejor estimación disponible sin inclinarse a ningún lado.
    ordenadas = sorted(lecturas)
    return ordenadas[len(ordenadas) // 2]


KEY_SEGMENTO_SEG = 90  # cuánto del medio del tema se analiza para la tonalidad

# Perfiles de Krumhansl-Schmuckler: fuerza típica de cada nota de la
# escala (empezando en la tónica) para mayor y para menor. Método
# clásico de detección de tonalidad por correlación de cromas.
_PERFIL_MAYOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_PERFIL_MENOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

# Nota tónica (0=Do, 1=Do#, 2=Re, ... 11=Si) -> código Camelot, según la
# rueda estándar (Mixed In Key / Rekordbox / Serato).
_CAMELOT_MAYOR = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
_CAMELOT_MENOR = {
    9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A",
    3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A",
}


def detectar_key(contenido_mp3: bytes, duracion_seg: int | None = None) -> str | None:
    """
    Estima la tonalidad en notación Camelot (p.ej. "8A", "5B") con el
    método de Krumhansl-Schmuckler: promedia la energía de cada nota
    (croma) en un segmento del medio del tema — evita el principio/final,
    más propensos a silencios o intros sin tonalidad clara — y la
    correlaciona contra los 24 perfiles posibles (12 tónicas x
    mayor/menor), quedándose con el que mejor correlaciona.

    A diferencia del BPM, acá no hace falta votación por puntos: con un
    segmento largo del medio del tema alcanza para una estimación
    estable. No es infalible — mayor y su relativo menor comparten las
    mismas notas, así que a veces puede confundir uno con el otro — pero
    da un punto de partida razonable, siempre editable a mano.

    `duracion_seg` es opcional (evita correr ffprobe de nuevo si ya se
    calculó antes). Devuelve None si falta librosa/ffmpeg o no se pudo
    leer nada del archivo.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        return None

    if not _hay_herramienta("ffmpeg"):
        return None

    with _archivo_temporal(contenido_mp3) as ruta:
        total = duracion_seg if duracion_seg is not None else obtener_duracion_seg(contenido_mp3)
        duracion_analisis = min(KEY_SEGMENTO_SEG, total) if total else KEY_SEGMENTO_SEG
        offset = max(0.0, (total - duracion_analisis) / 2) if total else 0.0

        wav_bytes = _extraer_segmento_wav(ruta, offset, duracion_analisis)
        if not wav_bytes:
            return None

        try:
            y, sr = librosa.load(io.BytesIO(wav_bytes), sr=22050, mono=True)
            if y.size == 0:
                return None
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            perfil_detectado = chroma.mean(axis=1)
        except Exception:
            logger.exception("Falló la detección de tonalidad")
            return None

    perfil_mayor = np.array(_PERFIL_MAYOR)
    perfil_menor = np.array(_PERFIL_MENOR)

    mejor_corr = -2.0
    mejor_camelot: str | None = None
    for tonica in range(12):
        corr_mayor = float(np.corrcoef(perfil_detectado, np.roll(perfil_mayor, tonica))[0, 1])
        corr_menor = float(np.corrcoef(perfil_detectado, np.roll(perfil_menor, tonica))[0, 1])
        if corr_mayor > mejor_corr:
            mejor_corr = corr_mayor
            mejor_camelot = _CAMELOT_MAYOR[tonica]
        if corr_menor > mejor_corr:
            mejor_corr = corr_menor
            mejor_camelot = _CAMELOT_MENOR[tonica]

    return mejor_camelot


def calcular_fingerprint(contenido_mp3: bytes) -> str | None:
    """
    Fingerprint de Chromaprint (fpcalc) para detectar el mismo audio
    subido con otro nombre/metadata. None si fpcalc no está disponible.
    """
    if not _hay_herramienta("fpcalc"):
        return None

    with _archivo_temporal(contenido_mp3) as ruta:
        resultado = subprocess.run(
            ["fpcalc", "-json", ruta],
            capture_output=True,
        )
        if resultado.returncode != 0:
            return None
        try:
            data = json.loads(resultado.stdout)
            return data.get("fingerprint")
        except json.JSONDecodeError:
            return None


def extraer_caratula(contenido_mp3: bytes) -> tuple[bytes, str] | None:
    """
    Extrae la carátula embebida (tag ID3 APIC) del MP3, si el archivo
    original traía una. Devuelve (bytes_de_la_imagen, extension) o
    None si no hay carátula, o si falta la librería mutagen.
    """
    try:
        from mutagen.id3 import APIC
        from mutagen.mp3 import MP3
    except ImportError:
        logger.warning("mutagen no está instalado — no se puede extraer la carátula embebida.")
        return None

    with _archivo_temporal(contenido_mp3) as ruta:
        try:
            tags = MP3(ruta).tags
        except Exception:
            return None
        if not tags:
            return None
        for tag in tags.values():
            if isinstance(tag, APIC):
                mime = (tag.mime or "").lower()
                extension = "png" if "png" in mime else "jpg"
                return bytes(tag.data), extension
    return None


def analizar_track(contenido_mp3: bytes) -> dict:
    """Corre duración + BPM + tonalidad + fingerprint de una. Pensado para /tracks/analyze."""
    duracion = obtener_duracion_seg(contenido_mp3)
    return {
        "duracion_seg": duracion,
        "bpm_detectado": detectar_bpm(contenido_mp3, duracion_seg=duracion),
        "key_detectada": detectar_key(contenido_mp3, duracion_seg=duracion),
        "fingerprint": calcular_fingerprint(contenido_mp3),
    }
