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

IMPORTANTE sobre memoria: todas las funciones de acá trabajan a partir de
una RUTA en disco (Path), no de los bytes del audio en memoria. Con
sesiones largas (~1 hora, ~200MB) cargar el archivo completo como bytes
de Python — más la copia transcodificada, más lo que usa cada subproceso
(ffmpeg/fpcalc) — podía superar fácil los 512MB de RAM del plan gratuito
de Render y tirar abajo el proceso a mitad de análisis (eso se veía como
"No se pudo conectar con el servidor" en el navegador, sin ningún error
claro de por medio). Pasando rutas en vez de bytes, el archivo vive en
disco todo el tiempo y cada herramienta externa lo lee directo — la
memoria de Python ya no crece con el tamaño del archivo.
"""

import io
import json
import logging
import math
import re
import resource
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("clubpack.audio")


def _hay_herramienta(nombre: str) -> bool:
    return shutil.which(nombre) is not None


def _memoria_mb() -> str:
    """
    Memoria residente PICO hasta este momento (nunca baja, aunque el uso
    real actual sea menor) — para loguear en cada paso de /tracks/analyze
    y así, si el proceso se cae por falta de memoria, los logs de Render
    ya muestren en qué paso venía creciendo en vez de tener que adivinar
    de nuevo (ver el megamix de 58MB/64min que se cayó acá sin que
    ninguno de los pasos de este archivo pareciera, por su cuenta, capaz
    de explicarlo — con esto, la próxima vez que pase se va a ver clarito
    cuál fue).

    RUSAGE_SELF: este proceso Python (ru_maxrss viene en KB en Linux).
    RUSAGE_CHILDREN: el subproceso con más memoria entre los que ya
    terminaron (ffmpeg, fpcalc) — Render mata el contenedor entero si la
    suma de TODOS los procesos supera el límite, no solo este proceso.
    """
    propio = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    hijos = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    return f"proceso={propio:.0f}MB pico, subprocesos={hijos:.0f}MB pico"


def precalentar_librosa() -> None:
    """
    Fuerza, con audio de mentira (2 segundos de silencio), los mismos dos
    caminos de librosa que usan detectar_bpm (beat_track) y detectar_key
    (chroma_cqt) — pensado para llamarse UNA vez al arrancar el server
    (ver app/main.py), antes de aceptar pedidos reales.

    Por qué existe esto: ninguna de las dos funciones tarda nada por sí
    misma con un clip de 2 segundos — lo que es lento (y pesado) es lo que
    pasa la PRIMERA vez que se llama a cualquiera de las dos en la vida del
    proceso: numba (que libROSA usa para acelerar beat_track/chroma_cqt)
    recién ahí importa y prepara su máquina de compilación JIT basada en
    LLVM (numba + llvmlite + su cache de compilación), y eso solo —
    medido en un entorno comparable — agrega entre 200 y 350MB a la
    memoria residente del proceso Python. Ese costo es FIJO (no depende
    del tamaño ni duración del audio real: pasa igual con un clip de 2
    segundos) y queda en memoria para siempre una vez pagado (los módulos
    de Python no se "desimportan").

    El problema no era entonces el tamaño del archivo subido en sí
    (eso ya se resolvió escribiéndolo a disco en vez de bytes en memoria,
    ver la nota al principio de este archivo) — era que ese golpe de
    ~300MB de numba pasaba recién la PRIMERA VEZ que alguien subía un
    track y llegaba a detectar_bpm/detectar_key, que en el plan gratuito
    de Render (512MB en total para TODO el proceso) coincidía justo con
    el peor momento posible: mientras el archivo transcodificado (hasta
    ~200MB) seguía en disco y el proceso ya venía de escribir/leer esa
    cantidad de datos. Haciendo este "precalentamiento" al arrancar el
    server — sin ningún archivo real en juego, con el proceso recién
    iniciado y memoria de sobra — ese costo fijo queda pagado de una vez,
    en un momento tranquilo, y cada subida real que llega después ya no
    tiene que pagarlo (solo le queda el costo chico y proporcional de
    analizar SU propio segmento de audio).

    Si algo falla acá (falta numpy/librosa, o lo que sea), no tiene que
    tirar abajo el arranque del server — en el peor caso, detectar_bpm/
    detectar_key van a pagar este mismo costo más tarde, en el primer
    pedido real, que es exactamente la situación que esto busca evitar,
    pero no es motivo para que el server ni arranque.
    """
    try:
        import warnings

        import numpy as np

        import librosa

        # 4 segundos de silencio: alcanza para que chroma_cqt no se queje
        # de que la ventana de FFT es más grande que la señal (eso sí
        # pasaba con menos). Los warnings que SÍ quedan (p.ej. "no se
        # puede estimar la afinación" — lógico, es silencio puro) no
        # importan para nada acá, así que se silencian para no ensuciar
        # los logs de arranque con ruido que no significa un problema.
        y = np.zeros(22050 * 4, dtype=np.float32)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            librosa.beat.beat_track(y=y, sr=22050)
            librosa.feature.chroma_cqt(y=y, sr=22050)
        logger.info(
            "Precalentamiento de librosa/numba listo (beat_track + chroma_cqt) — %s",
            _memoria_mb(),
        )
    except Exception:
        logger.exception(
            "No se pudo precalentar librosa/numba al arrancar — "
            "el primer análisis real va a pagar ese costo en su lugar."
        )


# Nivelado de volumen (EBU R128 / loudnorm de ffmpeg), para que un tema
# grabado bajo y uno masterizado fuerte no suenen a volúmenes muy
# distintos uno atrás del otro en una sesión de DJ:
#   -14 LUFS integrado  → el mismo estándar que usan Spotify/YouTube
#                         Music para música comercial; la mayoría de
#                         los temas de baile ya vienen masterizados
#                         cerca de ahí, así que el ajuste suele ser chico.
#   -1.0 dBTP de pico    → deja margen para que la reencodeada a MP3 no
#                         termine generando picos por encima de 0dBFS
#                         (los "picos entre muestras" que puede sumar
#                         cualquier códec con pérdida).
#   11 LU de rango       → el valor por default recomendado en la norma
#                         EBU R128, no comprime la dinámica del tema.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def _medir_loudness(ruta: Path) -> dict[str, float] | None:
    """
    Primera pasada de ffmpeg (solo mide, no genera ningún archivo): corre
    el filtro loudnorm en modo "print_format=json" para obtener la
    sonoridad real del tema (input_i/input_tp/input_lra/input_thresh) y
    cuánto ajuste haría falta (target_offset). Con esos datos, la
    segunda pasada (más abajo) puede aplicar un ajuste LINEAL de
    ganancia en vez de la compresión dinámica que aplicaría loudnorm a
    ciegas con una sola pasada — un solo corrimiento de volumen prolijo,
    sin tocarle la dinámica al tema.

    Devuelve None si no se pudo medir (archivo corrupto, completamente
    silencioso, etc.) — quien llama debe degradar con gracia a "no
    normalizar" en vez de romper la subida por esto.
    """
    resultado = subprocess.run(
        [
            "ffmpeg", "-i", str(ruta),
            "-af", f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
    )
    salida = resultado.stderr.decode(errors="ignore")
    coincidencia = re.search(r"\{\s*\"input_i\".*?\}", salida, re.DOTALL)
    if not coincidencia:
        logger.warning("No se pudo medir el volumen del audio (sin bloque JSON de loudnorm) — %s", salida[-300:])
        return None
    try:
        datos = json.loads(coincidencia.group(0))
        valores = {clave: float(valor) for clave, valor in datos.items() if clave != "normalization_type"}
    except (json.JSONDecodeError, ValueError):
        logger.warning("No se pudo interpretar la medición de volumen del audio — %s", coincidencia.group(0))
        return None

    # float() parsea "nan"/"-inf"/"inf" sin tirar ValueError — pasa con
    # un tema completamente silencioso (input_i da "-inf": no hay ningún
    # volumen que medir ni ganancia sensata que aplicar). Pasarle eso tal
    # cual a ffmpeg en la segunda pasada (más abajo) no degrada con
    # gracia: tira el filtro entero y rompe la conversión completa.
    if not all(math.isfinite(v) for v in valores.values()):
        logger.warning("Medición de volumen no numérica (tema silencioso o muy corto) — %s", valores)
        return None
    return valores


def transcodificar_a_mp3_320(ruta_entrada: Path, ruta_salida: Path) -> tuple[Path, str | None, bool]:
    """
    Convierte cualquier audio soportado por ffmpeg a MP3 CBR 320kbps,
    nivelando de paso el volumen a un estándar común (ver LOUDNORM_* acá
    arriba), leyendo desde `ruta_entrada` y escribiendo en `ruta_salida`
    (ambas ya en disco — nunca se cargan los bytes del audio en memoria
    de Python). Devuelve (ruta_resultante, advertencia_o_None, nivelado).

    `nivelado` (bool) dice si el nivelado de volumen se pudo aplicar de
    verdad — lo usa /tracks/confirm para guardar tracks.loudness_lufs
    (ver sql/023_nivelado_volumen.sql) solo cuando corresponde.

    Si ffmpeg no está disponible, deja `ruta_entrada` tal cual (sin
    convertir ni nivelar) y avisa — para no romper el flujo en un
    entorno sin ffmpeg. Si ffmpeg sí está pero no se pudo medir el
    volumen (ver _medir_loudness), se sigue convirtiendo a MP3 320 igual
    que antes, simplemente sin el nivelado — tampoco es motivo para
    romper la subida.
    """
    if not _hay_herramienta("ffmpeg"):
        aviso = "ffmpeg no está instalado en este servidor — se subió el archivo sin convertir a MP3 320."
        logger.warning(aviso)
        return ruta_entrada, aviso, False

    medicion = _medir_loudness(ruta_entrada)
    logger.info("transcodificar_a_mp3_320: medición de volumen lista — %s", _memoria_mb())

    filtro_audio = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
    if medicion:
        # Segunda pasada con los valores medidos (linear=true): un único
        # corrimiento de ganancia prolijo. Si el corrimiento necesario
        # fuera demasiado grande para hacerse sin recortar picos, ffmpeg
        # cae solo a modo dinámico (gating) en vez de romper — ver su
        # propia doc del filtro loudnorm.
        filtro_audio += (
            f":measured_I={medicion['input_i']}:measured_TP={medicion['input_tp']}"
            f":measured_LRA={medicion['input_lra']}:measured_thresh={medicion['input_thresh']}"
            f":offset={medicion['target_offset']}:linear=true"
        )

    resultado = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(ruta_entrada),
            "-af", filtro_audio,
            "-codec:a", "libmp3lame", "-b:a", "320k",
            # "-codec:v copy" conserva la carátula embebida (si el
            # archivo original trae una) como "attached_pic" en el
            # MP3 de salida, para poder extraerla después con
            # extraer_caratula(). Si no hay stream de video, ffmpeg
            # simplemente ignora el flag — no rompe nada.
            "-codec:v", "copy", "-id3v2_version", "3",
            str(ruta_salida),
        ],
        # El audio convertido va directo al archivo (ruta_salida) — ffmpeg
        # no escribe nada útil en stdout acá, así que no hace falta
        # bufferearlo (stdout=DEVNULL). stderr sí se guarda: es lo único
        # que tenemos para el mensaje de error si la conversión falla.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if resultado.returncode != 0 or not ruta_salida.exists():
        raise RuntimeError(
            f"No se pudo convertir el audio a MP3 320kbps: {resultado.stderr.decode(errors='ignore')[-500:]}"
        )
    logger.info("transcodificar_a_mp3_320: listo (nivelado=%s) — %s", bool(medicion), _memoria_mb())
    return ruta_salida, None, bool(medicion)


def obtener_duracion_seg(ruta_mp3: Path) -> int | None:
    """Duración en segundos usando ffprobe. None si no se puede calcular."""
    if not _hay_herramienta("ffprobe"):
        return None

    resultado = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(ruta_mp3),
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


def _extraer_segmento_wav(ruta_mp3: Path, offset_seg: float, duracion_seg: float) -> bytes | None:
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
            "-i", str(ruta_mp3),
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


def _tempo_en(ruta: Path, offset_seg: float) -> float | None:
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


def detectar_bpm(ruta_mp3: Path, duracion_seg: int | None = None) -> float | None:
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

    total = duracion_seg if duracion_seg is not None else obtener_duracion_seg(ruta_mp3)
    if not total or total < BPM_DURACION_MINIMA_PARA_3_PUNTOS:
        puntos = [0.0]
    else:
        puntos = [total * 0.25, total * 0.50, total * 0.75]

    lecturas = [b for b in (_tempo_en(ruta_mp3, p) for p in puntos) if b is not None]

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


def detectar_key(ruta_mp3: Path, duracion_seg: int | None = None) -> str | None:
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

    total = duracion_seg if duracion_seg is not None else obtener_duracion_seg(ruta_mp3)
    duracion_analisis = min(KEY_SEGMENTO_SEG, total) if total else KEY_SEGMENTO_SEG
    offset = max(0.0, (total - duracion_analisis) / 2) if total else 0.0

    wav_bytes = _extraer_segmento_wav(ruta_mp3, offset, duracion_analisis)
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


PEAKS_CANTIDAD_PUNTOS = 1200  # fijo sin importar la duración — alcanza para que se vea prolijo en cualquier ancho
PEAKS_SAMPLE_RATE = 4000  # bajísimo a propósito: solo hace falta la ENVOLVENTE de volumen, no la señal real


def calcular_peaks(ruta_mp3: Path) -> list[float] | None:
    """
    Calcula una "huella" simplificada del volumen de TODO el tema — el
    pico de amplitud en cada uno de PEAKS_CANTIDAD_PUNTOS tramos iguales
    — para que el player pueda dibujar la onda y arrancar a reproducir
    SIN tener que bajar y decodificar el archivo entero primero (eso es
    lo que hace wavesurfer.js por su cuenta si no le pasamos esto — ver
    Fetcher.fetchBlob/Decoder.decode en loadAudio(), en su código fuente:
    solo se saltea ese paso si se le pasa channelData ya calculado).

    A diferencia de detectar_bpm/detectar_key (que miran nada más un
    segmento corto), ACÁ sí hace falta recorrer el tema entero — es la
    envolvente de TODO el audio. Para no repetir el golpe de memoria de
    decodificar el archivo completo (ver nota de memoria al principio de
    este archivo), NO se usa librosa.load (decodifica a float32 a
    22050Hz — pesado para sesiones largas, y encima dispara el import
    pesado de numba) sino ffmpeg derecho a una resolución bajísima
    (4000Hz, mono, 16 bits): alcanza de sobra para una envolvente de
    volumen, no hace falta fidelidad de audio real. Una sesión de 1 hora
    a esa resolución pesa ~29MB mientras se calcula esto — bien lejos
    del límite de 512MB del plan gratis de Render.

    Devuelve una lista de ~1200 floats entre 0 y 1 (redondeados a 3
    decimales, para no inflar de más el JSON que se guarda — con esto
    cada track pesa unos 4-10KB en la columna `peaks` de la tabla
    tracks), o None si falta ffmpeg/numpy o no se pudo leer nada.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    if not _hay_herramienta("ffmpeg"):
        return None

    resultado = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(ruta_mp3),
            "-ar", str(PEAKS_SAMPLE_RATE), "-ac", "1", "-f", "s16le", "pipe:1",
        ],
        capture_output=True,
    )
    if resultado.returncode != 0 or not resultado.stdout:
        logger.warning(
            "ffmpeg no pudo decodificar el audio para calcular los peaks: %s",
            resultado.stderr.decode(errors="ignore")[-300:],
        )
        return None

    try:
        # Antes esto hacía .astype(np.float32) sobre el array ENTERO antes
        # de recortarlo en tramos — para una sesión larga eso duplicaba en
        # memoria el buffer completo (int16 + la copia float32 del mismo
        # tamaño), encima de todo lo demás que ya está en juego en esta
        # misma request (ver /tracks/confirm). Dejando `muestras` como
        # vista int16 (sin copiar) y convirtiendo recién cada tramo chico
        # al sacarle el máximo, el costo extra queda acotado al tamaño de
        # UN tramo (acá, ~1/1200 del archivo) en vez de al archivo entero.
        muestras = np.frombuffer(resultado.stdout, dtype="<i2")
        if muestras.size == 0:
            return None

        cantidad = min(PEAKS_CANTIDAD_PUNTOS, muestras.size)
        bordes = np.linspace(0, muestras.size, cantidad + 1, dtype=np.int64)
        picos = [
            float(np.abs(muestras[bordes[i]:bordes[i + 1]]).max()) / 32768.0
            if bordes[i + 1] > bordes[i]
            else 0.0
            for i in range(cantidad)
        ]
        return [round(p, 3) for p in picos]
    except Exception:
        logger.exception("Falló el cálculo de peaks del waveform")
        return None


def calcular_fingerprint(ruta_mp3: Path) -> str | None:
    """
    Fingerprint de Chromaprint (fpcalc) para detectar el mismo audio
    subido con otro nombre/metadata. None si fpcalc no está disponible.
    """
    if not _hay_herramienta("fpcalc"):
        return None

    resultado = subprocess.run(
        ["fpcalc", "-json", str(ruta_mp3)],
        capture_output=True,
    )
    if resultado.returncode != 0:
        return None
    try:
        data = json.loads(resultado.stdout)
        return data.get("fingerprint")
    except json.JSONDecodeError:
        return None


def extraer_caratula(ruta_mp3: Path) -> tuple[bytes, str] | None:
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

    try:
        tags = MP3(ruta_mp3).tags
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


# Ruta fija dentro de la imagen de Docker — ver app/assets/portada_generica.png
# (renderizada desde public/portada-generica.svg del frontend; si ese SVG
# cambia de diseño, hay que volver a renderizar y pisar este PNG a mano,
# no se regeneran solos porque viven en repos separados).
_RUTA_CARATULA_GENERICA = Path(__file__).parent / "assets" / "portada_generica.png"


def fijar_caratula_generica(ruta_mp3: Path) -> None:
    """
    Reemplaza (o agrega) la carátula embebida (tag ID3 APIC) de un MP3
    por la portada genérica de PLAYLIST, sacando cualquier carátula que
    trajera el archivo original.

    Se usa en /tracks/confirm cuando el usuario elige NO mantener la
    carátula original (usar_caratula_original=False) — antes esa
    elección solo cambiaba lo que se ve en el catálogo web
    (tracks.portada_url), pero el archivo que se descarga seguía
    trayendo tal cual la carátula vieja (a veces de otro servicio, mala
    calidad), lo cual no coincidía con lo elegido al subir. Se hace
    UNA sola vez acá, antes de subir el archivo a R2 — no en cada
    descarga — para no sumarle trabajo al servidor gratuito de Render
    por cada bajada (mismo motivo que zip_utils ya no arma ZIPs en el
    backend, ver el comentario grande ahí).

    Si algo falla (mutagen ausente, archivo raro) no tira excepción:
    loguea y deja el archivo tal cual estaba — quien llama decide si
    eso alcanza para no romper la subida (ver tracks.py, mismo patrón
    que duplicados/carátula más arriba).
    """
    try:
        from mutagen.id3 import APIC, ID3
        from mutagen.mp3 import MP3
    except ImportError:
        logger.warning("mutagen no está instalado — no se puede fijar la carátula genérica.")
        return

    try:
        imagen_bytes = _RUTA_CARATULA_GENERICA.read_bytes()
    except OSError:
        logger.exception("No se encontró %s — no se pudo fijar la carátula genérica.", _RUTA_CARATULA_GENERICA)
        return

    try:
        audio_tags = MP3(ruta_mp3, ID3=ID3)
        if audio_tags.tags is None:
            audio_tags.add_tags()
        audio_tags.tags.delall("APIC")  # saca cualquier carátula vieja (puede haber más de una)
        audio_tags.tags.add(
            APIC(
                encoding=3,  # UTF-8
                mime="image/png",
                type=3,  # "Cover (front)"
                desc="Cover",
                data=imagen_bytes,
            )
        )
        audio_tags.save(ruta_mp3, v2_version=3)
    except Exception:
        logger.exception("No se pudo fijar la carátula genérica en %s", ruta_mp3)


def analizar_track(ruta_mp3: Path) -> dict:
    """
    Corre duración + BPM + tonalidad + fingerprint de una. Pensado para
    /tracks/analyze. Logueado paso a paso a propósito: con el plan
    gratuito de Render (512MB) un megamix grande (58MB/64min) tiró abajo
    el proceso justo durante este análisis sin que ninguno de estos pasos
    pareciera, por su cuenta, capaz de explicarlo del todo — con el pico
    de memoria logueado en cada paso, si se repite va a quedar clarísimo
    cuál fue en vez de tener que adivinar de nuevo.
    """
    logger.info("analizar_track: inicio — %s", _memoria_mb())
    duracion = obtener_duracion_seg(ruta_mp3)
    logger.info("analizar_track: duración=%ss — %s", duracion, _memoria_mb())
    bpm = detectar_bpm(ruta_mp3, duracion_seg=duracion)
    logger.info("analizar_track: bpm=%s — %s", bpm, _memoria_mb())
    key = detectar_key(ruta_mp3, duracion_seg=duracion)
    logger.info("analizar_track: key=%s — %s", key, _memoria_mb())
    fingerprint = calcular_fingerprint(ruta_mp3)
    logger.info("analizar_track: fingerprint listo — %s", _memoria_mb())
    return {
        "duracion_seg": duracion,
        "bpm_detectado": bpm,
        "key_detectada": key,
        "fingerprint": fingerprint,
    }
