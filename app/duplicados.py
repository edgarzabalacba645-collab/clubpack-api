"""
Detección de duplicados en el catálogo.

Dos mecanismos:
1. Chequeo rápido al analizar un track (antes de confirmar la subida):
   compara texto (título + remixer) y audio contra lo que ya existe.
2. Escaneo completo (admin): recorre todo el catálogo y detecta tanto
   texto similar como audio similar, guardando los pares sospechosos en
   duplicados_detectados para revisión manual.

Portado de utils/duplicados.py del prototipo en Streamlit. Diferencia
clave: acá el cliente de Supabase se recibe por parámetro (ya autenticado
como el usuario que llama a la API en cada request), en vez de un
`get_client()` global atado a st.session_state.
"""

import numpy as np
from supabase import Client
from rapidfuzz import fuzz, process

try:
    import chromaprint

    _CHROMAPRINT_DISPONIBLE = True
except OSError:
    # Falta la librería compartida de chromaprint (libchromaprint1) en
    # este entorno — degrada con gracia: el chequeo de AUDIO simplemente
    # no corre (el de texto sigue andando) en vez de romper todo el
    # análisis de la subida. Ver Dockerfile (libchromaprint-tools).
    _CHROMAPRINT_DISPONIBLE = False

# Tabla de popcount (bits en 1) de cada valor posible de un byte —
# calculada una sola vez al importar el módulo. _similitud_audio la usa
# para contar bits de diferencia entre fingerprints con numpy en vez de
# con bin(x).count("1") en un bucle de Python puro, que con el catálogo
# real (cientos de temas, cada escaneo compara muchos pares) tardaba
# minutos — con esto, el mismo escaneo completo corre en segundos.
_TABLA_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount_uint32(valores: np.ndarray) -> np.ndarray:
    """Popcount de cada uint32 del arreglo, vectorizado: separa cada
    entero en sus 4 bytes y suma el popcount de cada uno (invariante al
    orden de bytes, así que no importa la endianness de la máquina)."""
    vista = valores.view(np.uint8).reshape(-1, 4)
    return _TABLA_POPCOUNT[vista].sum(axis=1, dtype=np.uint16)

UMBRAL_TEXTO_SOSPECHOSO = 85  # 0-100, similitud de texto para avisar

# 0-100, similitud ACÚSTICA (no igualdad exacta — ver _similitud_audio)
# para avisar. Calibrado con un caso real: el mismo audio con ~1s
# recortado al principio y otro bitrate dio 100%; dos temas realmente
# distintos dieron ~10% — 40 deja mucho margen de sobra para variaciones
# más fuertes (otro máster, un corte más largo) sin acercarse al ruido de
# fondo de temas no relacionados.
UMBRAL_AUDIO_SOSPECHOSO = 40.0

# Antes de comparar audio, se descartan los pares cuya duración difiere
# más que esto (segundos) — el mismo audio re-subido con un corte/edit
# levemente distinto sigue durando prácticamente lo mismo (el caso real
# que destapó esto: "Carita Triste/Olvidarte/A 200/¿Qué Pasaría (En
# Vivo)" vs la misma sin "(En Vivo)", 1 segundo de diferencia de
# duración). Comparar fingerprint por fingerprint SIN este filtro es
# carísimo en un catálogo de cientos de temas — cada comparación recorre
# miles de enteros — así que esto evita decodificar y comparar contra
# temas de duración totalmente distinta, que nunca iban a matchear igual.
VENTANA_DURACION_SEG = 10.0

# Mismos valores que usa la librería chromaprint/pyacoustid para este
# algoritmo: para cada posición del fingerprint más corto, busca la mejor
# alineación posible contra el más largo (tolerando que uno empiece unos
# "cuadros" antes/después que el otro), aceptando hasta MAX_BIT_ERROR bits
# de diferencia en cada posición comparada.
_MAX_ALIGN_OFFSET = 120
_MAX_BIT_ERROR = 2


def _texto_comparable(titulo: str, remixer: str | None) -> str:
    return f"{titulo} {remixer or ''}".strip().lower()


def _decodificar_fingerprint(fingerprint: str) -> np.ndarray | None:
    """
    Descomprime el fingerprint (tal cual lo guarda fpcalc en
    audio_fingerprint) a su arreglo de enteros original (como arreglo de
    numpy, para que _similitud_audio pueda compararlo rápido), para poder
    compararlo por similitud en vez de por igualdad de texto. None si
    falta la librería de chromaprint o el texto no se pudo decodificar
    (fingerprint corrupto o de un formato viejo).
    """
    if not _CHROMAPRINT_DISPONIBLE or not fingerprint:
        return None
    try:
        arreglo, _version = chromaprint.decode_fingerprint(fingerprint.encode("utf-8"))
        return np.array(arreglo, dtype=np.uint32)
    except Exception:
        return None


def _similitud_audio(a: np.ndarray, b: np.ndarray) -> float:
    """
    Similitud acústica entre dos fingerprints YA DECODIFICADOS (0-100).

    A diferencia de una comparación por igualdad, esto busca la mejor
    alineación temporal entre ambos — tolera que uno empiece unos
    segundos antes/después que el otro, un bitrate distinto, etc., que es
    justo lo que pasa con el mismo tema re-subido con un corte/edit
    levemente distinto (con igualdad de fingerprint exacta, eso NUNCA
    matchea, aunque sea literalmente el mismo audio).

    Mismo algoritmo y mismos resultados que una versión con dos bucles de
    Python anidados (probar, para cada posición i de `a`, contra cada
    posición j de `b` con |i-j| < MAX_ALIGN_OFFSET, y quedarse con el
    desplazamiento i-j que acumula más coincidencias) — pero reescrito
    para que cada desplazamiento se compare de una con numpy en vez de
    elemento por elemento en Python puro: con el catálogo real (cientos
    de temas) la versión en Python puro tardaba minutos por escaneo
    completo, esta tarda segundos.
    """
    largo_a, largo_b = len(a), len(b)
    if not largo_a or not largo_b:
        return 0.0
    mejor = 0
    # Mismo rango de desplazamientos que recorrería el doble bucle
    # original (|i-j| < MAX_ALIGN_OFFSET, con el límite superior
    # exclusivo igual que en range()).
    for desplazamiento in range(-_MAX_ALIGN_OFFSET + 1, _MAX_ALIGN_OFFSET + 1):
        if desplazamiento >= 0:
            n = min(largo_a - desplazamiento, largo_b)
            if n <= 0:
                continue
            xa = a[desplazamiento:desplazamiento + n]
            xb = b[:n]
        else:
            n = min(largo_a, largo_b + desplazamiento)
            if n <= 0:
                continue
            xa = a[:n]
            xb = b[-desplazamiento:-desplazamiento + n]
        errores = _popcount_uint32(xa ^ xb)
        coincidencias = int(np.count_nonzero(errores <= _MAX_BIT_ERROR))
        if coincidencias > mejor:
            mejor = coincidencias
    return 100 * mejor / min(largo_a, largo_b)


def chequear_antes_de_subir(client: Client, titulo: str, remixer: str, limite: int = 5) -> list[dict]:
    """
    Compara el título+remixer contra el catálogo existente y devuelve
    los candidatos más parecidos (para avisar antes de confirmar la subida).
    No toca la base de datos.
    """
    existentes = (
        client.table("tracks")
        .select("id, titulo, remixer")
        .eq("estado", "activo")
        .execute()
        .data
        or []
    )
    if not existentes:
        return []

    objetivo = _texto_comparable(titulo, remixer)
    candidatos = {
        t["id"]: _texto_comparable(t["titulo"], t.get("remixer"))
        for t in existentes
    }

    # token_set_ratio (no token_sort_ratio): un título que es el otro MÁS
    # algo extra ("(En Vivo)", "(Remix)", un signo de puntuación de más)
    # es el patrón de duplicado más común en la práctica — token_sort_ratio
    # penaliza esas palabras/símbolos de más aunque el resto sea idéntico
    # (confirmado con un caso real: 82%, por debajo del umbral);
    # token_set_ratio compara por el CONJUNTO de palabras compartidas y
    # da 95%+ en ese mismo caso.
    resultados = process.extract(
        objetivo, candidatos, scorer=fuzz.token_set_ratio, limit=limite
    )

    por_id = {t["id"]: t for t in existentes}
    salida = []
    for texto, score, track_id in resultados:
        if score >= UMBRAL_TEXTO_SOSPECHOSO:
            salida.append({**por_id[track_id], "similitud": round(score, 1)})
    return salida


def chequear_fingerprint_similar(
    client: Client, fingerprint: str | None, duracion_seg: float | None
) -> list[dict]:
    """
    Busca tracks activos cuyo audio sea ACÚSTICAMENTE similar (no
    necesariamente idéntico byte a byte) al fingerprint dado — se usa en
    /tracks/analyze para que un duplicado real salte ANTES de confirmar
    la subida, y no solo cuando el admin corre el escaneo completo del
    catálogo (ver escanear_catalogo_completo, más abajo).

    Antes esto comparaba el MD5 del fingerprint por IGUALDAD exacta, que
    solo agarraba el mismo archivo re-subido tal cual — cualquier re-
    encode, recorte de unos segundos al principio/final, etc. (lo más
    común en la práctica) se escapaba. Ahora decodifica y compara por
    similitud acústica real (ver _similitud_audio), igual que el escaneo
    completo — primero filtra por duración parecida (VENTANA_DURACION_SEG)
    antes de decodificar nada, que es muchísimo más barato que decodificar
    y comparar contra el catálogo entero en cada subida.
    """
    if not fingerprint or duracion_seg is None:
        return []
    decodificado_nuevo = _decodificar_fingerprint(fingerprint)
    if decodificado_nuevo is None:
        return []

    existentes = (
        client.table("tracks")
        .select("id, titulo, remixer, audio_fingerprint, duracion_seg")
        .eq("estado", "activo")
        .gte("duracion_seg", duracion_seg - VENTANA_DURACION_SEG)
        .lte("duracion_seg", duracion_seg + VENTANA_DURACION_SEG)
        .execute()
        .data
        or []
    )

    salida = []
    for t in existentes:
        decodificado_existente = _decodificar_fingerprint(t.get("audio_fingerprint"))
        if decodificado_existente is None:
            continue
        score = _similitud_audio(decodificado_nuevo, decodificado_existente)
        if score >= UMBRAL_AUDIO_SOSPECHOSO:
            salida.append({
                "id": t["id"],
                "titulo": t["titulo"],
                "remixer": t.get("remixer"),
                "similitud": round(score, 1),
            })
    salida.sort(key=lambda d: d["similitud"], reverse=True)
    return salida


def escanear_catalogo_completo(client: Client) -> dict:
    """
    Recorre TODO el catálogo activo y busca:
      - tracks con audio ACÚSTICAMENTE similar (ver _similitud_audio —
        no hace falta que sean idénticos byte a byte)
      - pares de título+remixer muy parecidos (posible duplicado)
    Guarda los hallazgos en duplicados_detectados (ignora los que ya
    estaban cargados, gracias al unique constraint de la tabla).
    Requiere que el cliente esté autenticado como admin (RLS lo exige
    para poder escribir en duplicados_detectados).
    """
    tracks = (
        client.table("tracks")
        .select("id, titulo, remixer, audio_fingerprint, duracion_seg")
        .eq("estado", "activo")
        .execute()
        .data
        or []
    )

    nuevos_fingerprint = 0
    nuevos_texto = 0

    # ---- 1. Audio similar ----
    # Comparar CADA PAR de fingerprints decodificados sería carísimo en
    # un catálogo de cientos de temas (cada comparación recorre miles de
    # enteros) — por eso se ordena por duración y cada tema solo se
    # compara contra los que le siguen DENTRO de VENTANA_DURACION_SEG:
    # el mismo audio re-subido con un corte/edit distinto sigue durando
    # prácticamente lo mismo, así que esto no deja pasar duplicados
    # reales y evita decodificar/comparar contra temas de duración
    # totalmente distinta, que nunca iban a matchear igual.
    con_audio = [
        t for t in tracks if t.get("audio_fingerprint") and t.get("duracion_seg") is not None
    ]
    con_audio.sort(key=lambda t: t["duracion_seg"])

    decodificados: dict[str, list[int] | None] = {}

    def _decodificado_cacheado(t: dict) -> list[int] | None:
        tid = t["id"]
        if tid not in decodificados:
            decodificados[tid] = _decodificar_fingerprint(t["audio_fingerprint"])
        return decodificados[tid]

    filas_fingerprint = []
    for i in range(len(con_audio)):
        t_a = con_audio[i]
        dec_a = _decodificado_cacheado(t_a)
        if dec_a is None:
            continue
        for j in range(i + 1, len(con_audio)):
            t_b = con_audio[j]
            if t_b["duracion_seg"] - t_a["duracion_seg"] > VENTANA_DURACION_SEG:
                break  # ordenado por duración: ninguno más adelante entra ya en la ventana
            dec_b = _decodificado_cacheado(t_b)
            if dec_b is None:
                continue
            score = _similitud_audio(dec_a, dec_b)
            if score >= UMBRAL_AUDIO_SOSPECHOSO:
                id_a, id_b = sorted((t_a["id"], t_b["id"]))
                filas_fingerprint.append({
                    "track_id_a": id_a,
                    "track_id_b": id_b,
                    "tipo": "fingerprint_similar",
                    "similitud": round(score, 1),
                })

    if filas_fingerprint:
        resp = client.table("duplicados_detectados").upsert(
            filas_fingerprint, on_conflict="track_id_a,track_id_b,tipo", ignore_duplicates=True
        ).execute()
        nuevos_fingerprint = len(resp.data or [])

    # ---- 2. Texto similar ----
    ids = [t["id"] for t in tracks]
    textos = [_texto_comparable(t["titulo"], t.get("remixer")) for t in tracks]

    if len(textos) >= 2:
        # token_set_ratio — ver el comentario en chequear_antes_de_subir
        # sobre por qué (títulos que son uno el otro + algo extra).
        matriz = process.cdist(textos, textos, scorer=fuzz.token_set_ratio, workers=-1)

        filas_texto = []
        vistos = set()
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                score = matriz[i][j]
                if score >= UMBRAL_TEXTO_SOSPECHOSO:
                    par = tuple(sorted((ids[i], ids[j])))
                    if par in vistos:
                        continue
                    vistos.add(par)
                    filas_texto.append({
                        "track_id_a": par[0],
                        "track_id_b": par[1],
                        "tipo": "texto_similar",
                        "similitud": round(float(score), 1),
                    })

        if filas_texto:
            resp = client.table("duplicados_detectados").upsert(
                filas_texto, on_conflict="track_id_a,track_id_b,tipo", ignore_duplicates=True
            ).execute()
            nuevos_texto = len(resp.data or [])

    return {
        "tracks_analizados": len(tracks),
        "nuevos_por_fingerprint": nuevos_fingerprint,
        "nuevos_por_texto": nuevos_texto,
    }


def listar_duplicados_pendientes(client: Client) -> list[dict]:
    resp = (
        client.table("duplicados_detectados")
        .select(
            "*, track_a:track_id_a(id, titulo, remixer, fecha_subida), "
            "track_b:track_id_b(id, titulo, remixer, fecha_subida)"
        )
        .eq("estado", "pendiente")
        .order("similitud", desc=True)
        .execute()
    )
    return resp.data or []


def marcar_duplicado(client: Client, dup_id: str, nuevo_estado: str):
    """nuevo_estado: 'descartado' (no es duplicado) o 'resuelto' (ya se borró uno)."""
    client.table("duplicados_detectados").update({"estado": nuevo_estado}).eq("id", dup_id).execute()
