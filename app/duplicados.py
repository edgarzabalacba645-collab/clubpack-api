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

import math

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


def _similitud_audio_lote(a: np.ndarray, candidatos: list[np.ndarray]) -> np.ndarray:
    """
    Similitud acústica (0-100) entre UN fingerprint `a` y VARIOS
    candidatos a la vez — misma definición que antes (busca, para cada
    candidato, la mejor alineación temporal tolerando que uno empiece
    unos "cuadros" antes/después que el otro, aceptando hasta
    MAX_BIT_ERROR bits de diferencia por posición), pero procesando
    TODOS los candidatos de `a` en una sola pasada de numpy en vez de
    una pasada por candidato.

    Por qué esto importa: ya con numpy por PAR, el escaneo completo
    seguía tardando varios minutos en producción (Render free tier) —
    no porque el cálculo en sí fuera lento, sino porque son muchas
    llamadas chicas a numpy (240 desplazamientos × miles de pares), y
    cada llamada tiene un costo fijo que en un entorno con CPU muy
    limitada pesa mucho más que en un entorno sin esa restricción.
    Agrupando todos los candidatos de un mismo tema en una sola matriz
    (uno por fila) se hacen las mismas 240 iteraciones UNA vez para
    todos sus candidatos juntos, en vez de una vez por candidato —
    medido: ~3-5x más rápido que comparar de a un par con la cantidad
    de candidatos típica por tema (decenas, por la ventana de duración).

    También usa np.bitwise_count (popcount nativo de numpy, disponible
    desde numpy 2.0) en vez de la tabla de popcount por bytes que tenía
    antes — es ~3 veces más rápido para esto.

    Devuelve un array de similitudes, en el mismo orden que `candidatos`
    (mismo resultado que llamar a esto con un solo candidato por vez).
    """
    if not candidatos:
        return np.array([], dtype=np.float64)

    largo_a = len(a)
    largos_b = np.array([len(b) for b in candidatos], dtype=np.int64)
    k = len(candidatos)
    if largo_a == 0:
        return np.zeros(k, dtype=np.float64)

    # Matriz (k, ancho): cada fila es un candidato, rellenado con ceros.
    # El relleno con ceros no afecta el resultado: esas posiciones se
    # excluyen explícitamente con la máscara `validez` más abajo (nunca
    # cuentan como coincidencia aunque por azar su popcount diera bajo).
    #
    # El ancho tiene que alcanzar para guardar el candidato más largo
    # ENTERO (max_largo_b) y, aparte, para que al desplazamiento más
    # negativo posible (-(MAX_ALIGN_OFFSET-1)) la porción que se lee de
    # `B` (que arranca en la columna -desplazamiento y se extiende
    # largo_a posiciones) tampoco se salga del arreglo — si no, numpy
    # recorta la slice en vez de tirar error y los arreglos de `a` y `b`
    # terminarían con formas distintas.
    max_largo_b = int(largos_b.max())
    ancho = max(max_largo_b, largo_a) + _MAX_ALIGN_OFFSET
    B = np.zeros((k, ancho), dtype=np.uint32)
    for idx, b in enumerate(candidatos):
        B[idx, :len(b)] = b

    mejor = np.zeros(k, dtype=np.int64)
    idx_b = np.arange(ancho)

    # Mismo rango de desplazamientos que el algoritmo por par (ver
    # docstring histórico: |i-j| < MAX_ALIGN_OFFSET).
    for desplazamiento in range(-_MAX_ALIGN_OFFSET + 1, _MAX_ALIGN_OFFSET + 1):
        if desplazamiento >= 0:
            n = largo_a - desplazamiento
            if n <= 0:
                continue
            xa = a[desplazamiento:desplazamiento + n]
            xb = B[:, :n]
            validez = idx_b[:n] < largos_b[:, None]
        else:
            n = largo_a
            inicio = -desplazamiento
            xa = a[:n]
            xb = B[:, inicio:inicio + n]
            validez = (idx_b[:n] + inicio) < largos_b[:, None]
        errores = np.bitwise_count(xb ^ xa[None, :])
        coincidencias = np.count_nonzero((errores <= _MAX_BIT_ERROR) & validez, axis=1)
        mejor = np.maximum(mejor, coincidencias)

    denominador = np.minimum(largo_a, largos_b)
    denominador = np.where(denominador == 0, 1, denominador)
    return 100 * mejor.astype(np.float64) / denominador


def _similitud_audio(a: np.ndarray, b: np.ndarray) -> float:
    """Similitud acústica entre DOS fingerprints ya decodificados (0-100)
    — casarse de a un par (ver _similitud_audio_lote para comparar uno
    contra varios de una, que es lo que usan los callers reales de
    abajo)."""
    return float(_similitud_audio_lote(a, [b])[0])


def chequear_antes_de_subir(client: Client, titulo: str, remixer: str, limite: int = 5) -> list[dict]:
    """
    Compara el título+remixer contra el catálogo existente y devuelve
    los candidatos más parecidos (para avisar antes de confirmar la subida).
    No toca la base de datos.
    """
    existentes = (
        client.table("tracks")
        # artista_original y generos(nombre): a pedido de Edgar, para que
        # el candidato de duplicado muestre Artista y Género además de
        # Remixer — generos(nombre) es el mismo embed de PostgREST que ya
        # se usa más abajo en listar_duplicados_pendientes (y en el
        # frontend, contra Supabase directo) para resolver una FK a su
        # nombre sin una consulta aparte.
        .select("id, titulo, remixer, artista_original, generos(nombre)")
        .eq("estado", "activo")
        .execute()
        .data
        or []
    )
    if not existentes:
        return []

    # PostgREST devuelve el embed como {"generos": {"nombre": "..."} o
    # None} — se aplana a un campo "genero" (string) ACÁ MISMO, antes de
    # armar nada más, así el resto de la función (y el auto-spread de
    # abajo que arma la salida final) no tiene que saber nada sobre el
    # formato de embeds de PostgREST, y el dict queda con la misma forma
    # que espera el schema DuplicadoCandidato.
    for t in existentes:
        generos_embebido = t.pop("generos", None)
        t["genero"] = (generos_embebido or {}).get("nombre")

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

    # `duracion_seg` en la tabla es INTEGER, pero acá llega como float (la
    # duración detectada del audio) — pasarle el float crudo a .gte()/.lte()
    # termina mandando algo como "271.0" a PostgREST, que Postgres rechaza
    # para una columna integer ("invalid input syntax for type integer:
    # '271.0'"): un literal con punto decimal no es un integer válido aunque
    # el valor entero coincida. Resultado real: CADA /tracks/analyze con un
    # audio real (duración siempre fraccionaria) tiraba esta excepción sin
    # capturar y la subida fallaba de punta a punta. floor/ceil en vez de
    # int() para no angostar la ventana por truncar siempre hacia 0.
    minimo = math.floor(duracion_seg - VENTANA_DURACION_SEG)
    maximo = math.ceil(duracion_seg + VENTANA_DURACION_SEG)
    existentes = (
        client.table("tracks")
        # artista_original y generos(nombre): mismo agregado que en
        # chequear_antes_de_subir, y por el mismo motivo (Artista y
        # Género al lado de Remixer en los candidatos de duplicado).
        .select("id, titulo, remixer, artista_original, generos(nombre), audio_fingerprint, duracion_seg")
        .eq("estado", "activo")
        .gte("duracion_seg", minimo)
        .lte("duracion_seg", maximo)
        .execute()
        .data
        or []
    )

    candidatos = []
    decodificados = []
    for t in existentes:
        decodificado_existente = _decodificar_fingerprint(t.get("audio_fingerprint"))
        if decodificado_existente is None:
            continue
        candidatos.append(t)
        decodificados.append(decodificado_existente)

    # Un solo llamado para todos los candidatos de esta subida (ver
    # _similitud_audio_lote) en vez de uno por candidato.
    scores = _similitud_audio_lote(decodificado_nuevo, decodificados)

    salida = []
    for t, score in zip(candidatos, scores):
        if score >= UMBRAL_AUDIO_SOSPECHOSO:
            # Esta función arma el dict de salida campo por campo (a
            # diferencia de chequear_antes_de_subir, que hace
            # {**por_id[track_id], ...}) — por eso artista_original y
            # genero se agregan explícitamente acá, no alcanza con
            # haberlos pedido en el .select() de arriba.
            generos_embebido = t.get("generos") or {}
            salida.append({
                "id": t["id"],
                "titulo": t["titulo"],
                "remixer": t.get("remixer"),
                "artista_original": t.get("artista_original"),
                "genero": generos_embebido.get("nombre"),
                "similitud": round(float(score), 1),
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

        # Candidatos de t_a dentro de la ventana de duración (como antes),
        # pero en vez de comparar de a uno, se decodifican todos y se
        # comparan los que decodificaron bien de UNA sola vez contra
        # dec_a (ver _similitud_audio_lote) — con catálogos grandes esto
        # es varias veces más rápido que un llamado a numpy por candidato.
        candidatos_b = []
        decodificados_b = []
        for j in range(i + 1, len(con_audio)):
            t_b = con_audio[j]
            if t_b["duracion_seg"] - t_a["duracion_seg"] > VENTANA_DURACION_SEG:
                break  # ordenado por duración: ninguno más adelante entra ya en la ventana
            dec_b = _decodificado_cacheado(t_b)
            if dec_b is None:
                continue
            candidatos_b.append(t_b)
            decodificados_b.append(dec_b)

        if not candidatos_b:
            continue

        scores = _similitud_audio_lote(dec_a, decodificados_b)
        for t_b, score in zip(candidatos_b, scores):
            if score >= UMBRAL_AUDIO_SOSPECHOSO:
                id_a, id_b = sorted((t_a["id"], t_b["id"]))
                filas_fingerprint.append({
                    "track_id_a": id_a,
                    "track_id_b": id_b,
                    "tipo": "fingerprint_similar",
                    "similitud": round(float(score), 1),
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
        #
        # workers=1 (NO -1/"todos los cores"): en Render free tier el
        # contenedor tiene una fracción de CPU (no un core entero), pero
        # os.cpu_count() adentro del contenedor puede devolver un número
        # más alto igual — pedirle a rapidfuzz "todos los cores" ahí
        # dispara más hilos/procesos de los que esa fracción de CPU puede
        # atender bien, y el sistema operativo los va frenando (CPU
        # throttling), lo que puede tardar muchísimo más que resolverlo
        # en un solo hilo. Con ~500 temas, de un solo hilo esto tarda
        # bien poco (rapidfuzz ya es C++ optimizado) — no hace falta
        # paralelizar, y sin este freno probablemente la causa del
        # escaneo quedándose colgado no era la comparación de audio.
        matriz = process.cdist(textos, textos, scorer=fuzz.token_set_ratio, workers=1)

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
