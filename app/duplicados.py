"""
Detección de duplicados en el catálogo.

Dos mecanismos:
1. Chequeo rápido al analizar un track (antes de confirmar la subida):
   compara texto (título + remixer) contra lo que ya existe.
2. Escaneo completo (admin): recorre todo el catálogo y detecta tanto
   texto similar como fingerprints de audio idénticos, guardando los
   pares sospechosos en duplicados_detectados para revisión manual.

Portado de utils/duplicados.py del prototipo en Streamlit. Diferencia
clave: acá el cliente de Supabase se recibe por parámetro (ya autenticado
como el usuario que llama a la API en cada request), en vez de un
`get_client()` global atado a st.session_state.
"""

from supabase import Client
from rapidfuzz import fuzz, process

UMBRAL_TEXTO_SOSPECHOSO = 85  # 0-100, similitud de texto para avisar


def _texto_comparable(titulo: str, remixer: str | None) -> str:
    return f"{titulo} {remixer or ''}".strip().lower()


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

    resultados = process.extract(
        objetivo, candidatos, scorer=fuzz.token_sort_ratio, limit=limite
    )

    por_id = {t["id"]: t for t in existentes}
    salida = []
    for texto, score, track_id in resultados:
        if score >= UMBRAL_TEXTO_SOSPECHOSO:
            salida.append({**por_id[track_id], "similitud": round(score, 1)})
    return salida


def escanear_catalogo_completo(client: Client) -> dict:
    """
    Recorre TODO el catálogo activo y busca:
      - tracks con el mismo audio_fingerprint (duplicado casi seguro)
      - pares de título+remixer muy parecidos (posible duplicado)
    Guarda los hallazgos en duplicados_detectados (ignora los que ya
    estaban cargados, gracias al unique constraint de la tabla).
    Requiere que el cliente esté autenticado como admin (RLS lo exige
    para poder escribir en duplicados_detectados).
    """
    tracks = (
        client.table("tracks")
        .select("id, titulo, remixer, audio_fingerprint")
        .eq("estado", "activo")
        .execute()
        .data
        or []
    )

    nuevos_fingerprint = 0
    nuevos_texto = 0

    # ---- 1. Fingerprint exacto ----
    por_fingerprint: dict[str, list[str]] = {}
    for t in tracks:
        fp = t.get("audio_fingerprint")
        if fp:
            por_fingerprint.setdefault(fp, []).append(t["id"])

    filas_fingerprint = []
    for ids in por_fingerprint.values():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                filas_fingerprint.append({
                    "track_id_a": ids[i],
                    "track_id_b": ids[j],
                    "tipo": "fingerprint_exacto",
                    "similitud": 100,
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
        matriz = process.cdist(textos, textos, scorer=fuzz.token_sort_ratio, workers=-1)

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
