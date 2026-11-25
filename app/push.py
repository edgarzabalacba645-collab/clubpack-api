"""
Envío de notificaciones push al navegador (Web Push + VAPID) — para
avisarle a un usuario que lo arrobaron en el chat aunque tenga la app
cerrada o el celular bloqueado.

Se dispara desde routers/webhooks.py (/webhooks/mencion-chat), que a su
vez lo llama un trigger de Postgres apenas se inserta un mensaje con
menciones (ver sql/021_notificaciones_push.sql) — no depende de que
nadie tenga el chat abierto en ese momento.

Por qué esto SÍ necesita una librería real (a diferencia del aviso de
Telegram, puro HTTP, ver app/telegram.py): cada mensaje hay que
encriptarlo distinto para cada suscripción (ECDH + AES-GCM con las
claves p256dh/auth propias de ESE navegador) y firmar un JWT VAPID
(ES256) — pywebpush hace las dos cosas.
"""

import json
import logging

from pywebpush import WebPushException, webpush

from . import deps
from .config import settings

logger = logging.getLogger("clubpack.push")


def enviar(suscripcion: dict, titulo: str, cuerpo: str, url: str = "/chat") -> bool:
    """
    Manda una notificación a UNA suscripción puntual (un navegador/
    dispositivo). `suscripcion` trae usuario_id/endpoint/p256dh/auth,
    tal cual los devuelve la función SQL obtener_suscripciones_push_de.
    Nunca tira — un fallo acá no debe romper el resto del envío a otras
    suscripciones (ver el loop en routers/webhooks.py).
    """
    if not settings.VAPID_PRIVATE_KEY:
        logger.warning("VAPID no está configurado (falta VAPID_PRIVATE_KEY) — no se manda la notificación push.")
        return False

    try:
        webpush(
            subscription_info={
                "endpoint": suscripcion["endpoint"],
                "keys": {"p256dh": suscripcion["p256dh"], "auth": suscripcion["auth"]},
            },
            data=json.dumps({"titulo": titulo, "cuerpo": cuerpo, "url": url}),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            # Si el celular está offline, no tiene sentido que el
            # servicio de push la guarde para siempre — 6hs alcanza de
            # sobra para una mención de chat.
            ttl=6 * 60 * 60,
        )
        return True
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        if status in (404, 410):
            # La suscripción ya no existe del lado del navegador
            # (se desinstaló la app, se revocó el permiso, etc.) —
            # se borra para no seguir intentando en vano cada vez.
            _borrar_suscripcion_muerta(suscripcion["endpoint"])
        else:
            logger.warning("Falló el envío de push a %s: %s", suscripcion.get("endpoint"), e)
        return False
    except Exception:
        logger.exception("Error inesperado mandando una notificación push")
        return False


def _borrar_suscripcion_muerta(endpoint: str) -> None:
    if not settings.BACKEND_INTERNAL_SECRET:
        return
    try:
        cliente = deps.cliente_para_webhook()
        cliente.rpc(
            "borrar_suscripcion_push",
            {"p_secret": settings.BACKEND_INTERNAL_SECRET, "p_endpoint": endpoint},
        ).execute()
    except Exception:
        logger.exception("No se pudo borrar la suscripción push muerta (%s)", endpoint)
