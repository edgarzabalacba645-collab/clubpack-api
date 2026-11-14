"""
Balance de ancho de banda (bandwidth) de Render para el panel de Admin —
ver routers/uso.py.

Por qué esto necesita otra fuente de datos: el plan gratis (Hobby) de
Render incluye un ancho de banda de salida limitado por mes, compartido
entre todos los servicios de la cuenta — al superarlo, Render empieza a
cobrar. Esos números reales solo están en la API de métricas de Render
(api.render.com), que necesita su propio token de cuenta
(RENDER_API_KEY, ver config.py), separado de cualquier otra credencial.
Igual que cloudflare_uso.py, si ese token no está configurado esto
degrada con gracia (devuelve un aviso) en vez de romper el resto de la
API.

Límite gratis y precio del plan Hobby de Render, al momento de escribir
esto (bajó de 100 GB a 5 GB durante 2026):
  - Incluido: 5 GB de ancho de banda de salida por mes, por cuenta.
  - Excedente: USD 0,15 por GB.

La API de métricas (`GET /v1/metrics/bandwidth`) devuelve una serie de
puntos por hora en megabytes (unit "mb") para el recurso pedido — acá se
suman todos los puntos desde el día 1 del mes en curso hasta ahora.
"""

import logging
import time
from datetime import datetime, timezone

import httpx

from .config import settings

logger = logging.getLogger("clubpack.render_uso")

_METRICS_URL = "https://api.render.com/v1/metrics/bandwidth"

_GB_GRATIS = 5
_USD_POR_GB_EXCEDENTE = 0.15

# Cache simple en memoria: lo mira un admin de vez en cuando, no hace
# falta golpear la API de Render en cada render del panel.
_cache: dict | None = None
_cache_hasta = 0.0
_CACHE_SEG = 300


def obtener_balance() -> dict:
    global _cache, _cache_hasta

    if not settings.RENDER_API_KEY or not settings.RENDER_SERVICE_ID:
        return {
            "configurado": False,
            "mensaje": (
                "Falta configurar RENDER_API_KEY y/o RENDER_SERVICE_ID en el servidor — "
                "generá una clave en Render (Account Settings > API Keys) y agregala, junto "
                "con el Service ID de este backend, como variables de entorno."
            ),
        }

    ahora = time.time()
    if _cache is not None and ahora < _cache_hasta:
        return _cache

    ahora_dt = datetime.now(timezone.utc)
    desde_dt = ahora_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    try:
        r = httpx.get(
            _METRICS_URL,
            headers={
                "Authorization": f"Bearer {settings.RENDER_API_KEY}",
                "Accept": "application/json",
            },
            params={
                "resource": settings.RENDER_SERVICE_ID,
                "startTime": desde_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": ahora_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            timeout=15,
        )
        r.raise_for_status()
        datos = r.json()
    except Exception:
        logger.exception("Falló la consulta a la API de métricas de Render")
        return {
            "configurado": True,
            "mensaje": "No se pudo consultar Render en este momento — probá de nuevo en unos minutos.",
        }

    valores = datos[0].get("values", []) if datos else []
    total_mb = sum(v.get("value", 0) for v in valores)
    gb_usados = total_mb / 1024

    excedente = max(0.0, gb_usados - _GB_GRATIS)
    costo_usd = round(excedente * _USD_POR_GB_EXCEDENTE, 2)
    porcentaje = round((gb_usados / _GB_GRATIS) * 100, 1) if _GB_GRATIS else 0.0

    resultado = {
        "configurado": True,
        "periodo_desde": desde_dt.strftime("%d/%m/%Y"),
        "periodo_hasta": ahora_dt.strftime("%d/%m/%Y"),
        "bandwidth": {
            "usado": round(gb_usados, 3),
            "limite_gratis": _GB_GRATIS,
            "porcentaje": porcentaje,
            "costo_usd": costo_usd,
        },
    }

    _cache = resultado
    _cache_hasta = ahora + _CACHE_SEG
    return resultado
