"""
Envío de avisos a Telegram vía su Bot API — HTTP directo, sin SDK.

Se usa para la alerta instantánea de nuevo registro (ver
routers/webhooks.py). Si no está configurado (falta token o chat id),
degrada con gracia: loguea y no manda nada, en vez de romper el flujo
que la llame.
"""

import logging

import httpx

from .config import settings

logger = logging.getLogger("clubpack.telegram")


def enviar_mensaje(texto: str) -> bool:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram no está configurado (falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID) — no se envía: %s", texto)
        return False

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception:
        logger.exception("Falló el envío del mensaje a Telegram")
        return False
