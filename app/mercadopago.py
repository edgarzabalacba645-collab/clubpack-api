"""
Integración con Mercado Pago: suscripción recurrente (Preapproval) y
validación de la firma de los webhooks.

Se llama directo a la API REST de Mercado Pago con httpx (en vez de usar
el SDK oficial `mercadopago` de Python) a propósito: es el mismo patrón
que ya usa este backend para R2 (boto3 directo, sin wrapper propio), y
evita depender de un nombre de método del SDK que no se pudo confirmar
con certeza contra la documentación oficial al escribir esto.

Referencia: https://www.mercadopago.com.ar/developers/es/docs/subscriptions
"""

import hashlib
import hmac
import logging
import time

import httpx

from .config import settings

logger = logging.getLogger("clubpack.mercadopago")

API_BASE = "https://api.mercadopago.com"


class MercadoPagoError(Exception):
    pass


def _headers() -> dict[str, str]:
    if not settings.MP_ACCESS_TOKEN:
        raise MercadoPagoError(
            "Mercado Pago no está configurado (falta MP_ACCESS_TOKEN en el .env del backend)."
        )
    return {
        "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def crear_preapproval(
    *,
    usuario_id: str,
    email: str,
    monto: float,
    moneda: str = "ARS",
    razon: str = "Suscripción mensual ClubPack",
) -> dict:
    """
    Crea una suscripción recurrente "sin plan asociado" (el usuario autoriza
    el cobro desde una página de Mercado Pago, no hace falta tokenizar la
    tarjeta en nuestro frontend). Devuelve el dict crudo de la respuesta de
    MP; lo que interesa es `init_point` (link de autorización en producción)
    y `sandbox_init_point` (en modo prueba) — ese link sirve tanto para el
    botón "Pagar con Mercado Pago" como para generar el QR.

    `external_reference` queda en el id de usuario de Supabase: así el
    webhook puede saber a quién activarle la suscripción.
    """
    body = {
        "reason": razon,
        "external_reference": usuario_id,
        "payer_email": email,
        "back_url": f"{settings.FRONTEND_URL}/suscripcion?resultado=mp",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": monto,
            "currency_id": moneda,
        },
        "status": "pending",
    }

    with httpx.Client(timeout=20.0) as cliente:
        resp = cliente.post(f"{API_BASE}/preapproval", headers=_headers(), json=body)

    if resp.status_code >= 300:
        logger.error("Mercado Pago rechazó la creación del preapproval: %s %s", resp.status_code, resp.text)
        raise MercadoPagoError(f"Mercado Pago respondió {resp.status_code}: {resp.text}")

    return resp.json()


def obtener_preapproval(preapproval_id: str) -> dict:
    """GET /preapproval/{id} — estado actual de una suscripción (authorized/paused/cancelled/pending)."""
    with httpx.Client(timeout=20.0) as cliente:
        resp = cliente.get(f"{API_BASE}/preapproval/{preapproval_id}", headers=_headers())

    if resp.status_code >= 300:
        raise MercadoPagoError(f"Mercado Pago respondió {resp.status_code}: {resp.text}")
    return resp.json()


def obtener_payment(payment_id: str) -> dict | None:
    """
    GET de un pago puntual (cada cobro mensual de la suscripción manda su
    propio evento de webhook, separado del de la suscripción en sí).
    Prueba primero el endpoint general de pagos; si no existe con ese id
    (puede pasar con `subscription_authorized_payment`), prueba el
    endpoint específico de cobros de suscripción antes de rendirse.
    """
    with httpx.Client(timeout=20.0) as cliente:
        resp = cliente.get(f"{API_BASE}/v1/payments/{payment_id}", headers=_headers())
        if resp.status_code < 300:
            return resp.json()

        resp2 = cliente.get(f"{API_BASE}/authorized_payments/{payment_id}", headers=_headers())
        if resp2.status_code < 300:
            return resp2.json()

    logger.warning(
        "No se pudo obtener el pago %s ni como /v1/payments ni como /authorized_payments (%s / %s)",
        payment_id,
        resp.status_code,
        resp2.status_code,
    )
    return None


def validar_firma_webhook(*, x_signature: str | None, x_request_id: str | None, data_id: str) -> bool:
    """
    Valida el header `x-signature` que manda Mercado Pago en cada webhook,
    para estar seguros de que la notificación viene de verdad de ellos y
    no de cualquiera que le pegue a esta URL.

    Formato del header: "ts=<timestamp>,v1=<hash>". El hash es un
    HMAC-SHA256, con la "clave secreta" del panel Webhooks como clave,
    sobre un manifest de texto con este formato:

        id:{data_id};request-id:{x_request_id};ts:{ts};

    Nota de implementación: la documentación pública de Mercado Pago no
    muestra el algoritmo paso a paso en la pestaña "Sin SDKs" (requiere
    JS para verse), así que este manifest se armó con la mejor referencia
    disponible y HAY QUE confirmarlo con el botón "Simular notificación"
    del panel de Webhooks antes de depender de esto en producción. Si la
    validación rechaza siempre una notificación simulada real, lo primero
    a revisar es el orden/formato exacto de este string.
    """
    if not settings.MP_WEBHOOK_SECRET:
        logger.error("MP_WEBHOOK_SECRET no está configurado — se rechaza el webhook por seguridad.")
        return False
    if not x_signature:
        return False

    partes = dict(
        p.split("=", 1) for p in x_signature.split(",") if "=" in p
    )
    ts = partes.get("ts")
    v1_recibido = partes.get("v1")
    if not ts or not v1_recibido:
        return False

    manifest = f"id:{data_id};request-id:{x_request_id or ''};ts:{ts};"
    v1_calculado = hmac.new(
        settings.MP_WEBHOOK_SECRET.encode("utf-8"),
        manifest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(v1_calculado, v1_recibido)


def ahora_unix() -> int:
    return int(time.time())
