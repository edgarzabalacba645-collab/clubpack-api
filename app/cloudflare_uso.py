"""
Balance de uso de Cloudflare R2 (storage + operaciones) para el panel de
Admin — ver routers/uso.py.

Por qué esto necesita otra fuente de datos: el cliente S3 que ya usa
storage.py (boto3) sirve para subir/bajar archivos, pero no expone
contadores de facturación. Los números REALES de cuánto se lleva usado
de cada límite gratuito (y cuánto costaría pasarse) solo están en la
GraphQL Analytics API de Cloudflare — un servicio aparte que necesita su
propio token (CLOUDFLARE_API_TOKEN, ver config.py), separado de las
credenciales de R2. Igual que telegram.py, si ese token no está
configurado esto degrada con gracia (devuelve un aviso) en vez de romper
el resto de la API.

Precios y límites gratuitos de R2 (Standard storage), al momento de
escribir esto — https://developers.cloudflare.com/r2/pricing/:
  - Storage: 10 GB-mes gratis, luego USD 0,015 por GB-mes.
  - Clase A (operaciones que escriben/mutan, ej. subir un track):
    1.000.000 gratis por mes, luego USD 4,50 por millón.
  - Clase B (operaciones que leen, ej. escuchar o bajar un track):
    10.000.000 gratis por mes, luego USD 0,36 por millón.
  - Egreso (bytes que salen a internet, lo que más pesa en escuchas y
    descargas): siempre gratis en R2, sin tope — por eso ni aparece
    como límite acá abajo.
"""

import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .config import settings

logger = logging.getLogger("clubpack.cloudflare_uso")

_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

_STORAGE_GB_GRATIS = 10
_STORAGE_USD_POR_GB = 0.015
_CLASE_A_GRATIS = 1_000_000
_CLASE_A_USD_POR_MILLON = 4.50
_CLASE_B_GRATIS = 10_000_000
_CLASE_B_USD_POR_MILLON = 0.36

# Qué actionType de la API de R2 cuenta como Clase A (escritura) vs
# Clase B (lectura) — misma página de precios de arriba. Lo que no está
# en ninguna de las dos dos listas (ej. DeleteObject) es gratis y no
# suma a ningún total.
_CLASE_A_ACTIONS = {
    "ListBuckets",
    "PutBucket",
    "ListObjects",
    "PutObject",
    "CopyObject",
    "CompleteMultipartUpload",
    "CreateMultipartUpload",
    "LifecycleStorageTierTransition",
    "ListMultipartUploads",
    "UploadPart",
    "UploadPartCopy",
    "ListParts",
    "PutBucketEncryption",
    "PutBucketCors",
    "PutBucketLifecycleConfiguration",
}
_CLASE_B_ACTIONS = {
    "HeadBucket",
    "HeadObject",
    "GetObject",
    "UsageSummary",
    "GetBucketEncryption",
    "GetBucketLocation",
    "GetBucketCors",
    "GetBucketLifecycleConfiguration",
}

# Cache simple en memoria: esto lo mira un admin de vez en cuando desde
# el panel, no hace falta golpear la API de Cloudflare en cada render
# (y las consultas GraphQL de analytics no son instantáneas).
_cache: dict | None = None
_cache_hasta = 0.0
_CACHE_SEG = 300

_QUERY_STORAGE = """
query($accountTag: string!, $start: Time!, $end: Time!, $bucket: string!) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2StorageAdaptiveGroups(
        limit: 1
        filter: { datetime_geq: $start, datetime_leq: $end, bucketName: $bucket }
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize objectCount }
      }
    }
  }
}
"""

_QUERY_OPERACIONES = """
query($accountTag: string!, $start: Time!, $end: Time!, $bucket: string!) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2OperationsAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $start, datetime_leq: $end, bucketName: $bucket }
      ) {
        sum { requests }
        dimensions { actionType }
      }
    }
  }
}
"""


def _account_id() -> str:
    """
    El Account ID de Cloudflare ya viaja adentro de R2_ENDPOINT_URL
    (https://<ACCOUNT_ID>.r2.cloudflarestorage.com) — se reaprovecha de
    ahí en vez de pedir otra variable de entorno más.
    """
    m = re.search(r"https?://([a-f0-9]{32})\.r2\.cloudflarestorage\.com", settings.R2_ENDPOINT_URL)
    return m.group(1) if m else ""


def _consultar(query: str, variables: dict) -> dict:
    r = httpx.post(
        _GRAPHQL_URL,
        headers={
            "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables},
        timeout=15,
    )
    r.raise_for_status()
    datos = r.json()
    if datos.get("errors"):
        raise RuntimeError(str(datos["errors"]))
    return datos["data"]


def _item(usado: float, gratis: float, costo_excedente) -> dict:
    excedente = max(0.0, usado - gratis)
    return {
        "usado": round(usado, 4),
        "limite_gratis": gratis,
        "porcentaje": round((usado / gratis) * 100, 1) if gratis else 0.0,
        "costo_usd": round(costo_excedente(excedente), 4),
    }


def obtener_balance() -> dict:
    global _cache, _cache_hasta

    if not settings.CLOUDFLARE_API_TOKEN:
        return {
            "configurado": False,
            "mensaje": (
                "Falta configurar CLOUDFLARE_API_TOKEN en el servidor — generá un token de "
                "solo lectura en Cloudflare (permiso de cuenta \"Account Analytics\": Read) y "
                "agregalo como variable de entorno en Render."
            ),
        }

    ahora = time.time()
    if _cache is not None and ahora < _cache_hasta:
        return _cache

    account_id = _account_id()
    if not account_id:
        return {
            "configurado": False,
            "mensaje": "No se pudo determinar el Account ID de Cloudflare a partir de R2_ENDPOINT_URL.",
        }

    ahora_dt = datetime.now(timezone.utc)
    desde_dt = ahora_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    variables = {
        "accountTag": account_id,
        "start": desde_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": ahora_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": settings.R2_BUCKET_NAME,
    }

    try:
        datos_storage = _consultar(_QUERY_STORAGE, variables)
        datos_ops = _consultar(_QUERY_OPERACIONES, variables)
    except Exception:
        logger.exception("Falló la consulta a la GraphQL Analytics API de Cloudflare")
        return {
            "configurado": True,
            "mensaje": "No se pudo consultar Cloudflare en este momento — probá de nuevo en unos minutos.",
        }

    grupos_storage = datos_storage["viewer"]["accounts"][0]["r2StorageAdaptiveGroups"]
    if grupos_storage:
        maxs = grupos_storage[0]["max"]
        bytes_usados = (maxs.get("payloadSize") or 0) + (maxs.get("metadataSize") or 0)
    else:
        bytes_usados = 0
    gb_usados = bytes_usados / (1024**3)

    grupos_ops = datos_ops["viewer"]["accounts"][0]["r2OperationsAdaptiveGroups"]
    clase_a = 0
    clase_b = 0
    for g in grupos_ops:
        accion = g["dimensions"]["actionType"]
        cantidad = g["sum"]["requests"]
        if accion in _CLASE_A_ACTIONS:
            clase_a += cantidad
        elif accion in _CLASE_B_ACTIONS:
            clase_b += cantidad
        # lo que no es ni A ni B (ej. DeleteObject) es gratis, no suma

    item_storage = _item(gb_usados, _STORAGE_GB_GRATIS, lambda ex: ex * _STORAGE_USD_POR_GB)
    item_a = _item(clase_a, _CLASE_A_GRATIS, lambda ex: (ex / 1_000_000) * _CLASE_A_USD_POR_MILLON)
    item_b = _item(clase_b, _CLASE_B_GRATIS, lambda ex: (ex / 1_000_000) * _CLASE_B_USD_POR_MILLON)

    resultado = {
        "configurado": True,
        "periodo_desde": desde_dt.strftime("%d/%m/%Y"),
        "periodo_hasta": ahora_dt.strftime("%d/%m/%Y"),
        "storage": item_storage,
        "operaciones_a": item_a,
        "operaciones_b": item_b,
        "costo_total_usd": round(
            item_storage["costo_usd"] + item_a["costo_usd"] + item_b["costo_usd"], 4
        ),
    }

    _cache = resultado
    _cache_hasta = ahora + _CACHE_SEG
    return resultado
