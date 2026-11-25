"""
Endpoints llamados por servicios externos, SIN JWT de usuario (no hay
sesión de la app corriendo). Se protegen con un secreto compartido en
un header propio — nunca con la service_role key de Supabase.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .. import deps, push, telegram
from ..config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("clubpack.webhooks")


@router.post("/nuevo-registro")
async def nuevo_registro(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    Lo llama un Database Webhook de Supabase (Database > Webhooks, en
    el dashboard) configurado sobre INSERT sobre public.perfiles — es
    decir, se dispara cada vez que alguien se registra (el trigger
    handle_new_user crea esa fila al instante). Manda un aviso a
    Telegram para que el admin lo pueda gestionar apenas entra.

    No usa Authorization: Bearer porque quien llama no es un usuario
    logueado de la app — por eso el secreto va en un header propio,
    que se configura al crear el webhook en Supabase.
    """
    if not settings.REGISTRO_WEBHOOK_SECRET or x_webhook_secret != settings.REGISTRO_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secreto inválido")

    try:
        body = await request.json()
    except Exception:
        body = {}

    fila = body.get("record") or {}
    email = fila.get("email") or "(sin email)"
    nombre_dj = fila.get("nombre_dj")

    quien = f"{nombre_dj} ({email})" if nombre_dj and nombre_dj != email else email
    texto = (
        f"🆕 <b>Nuevo registro en PLAYLIST</b>\n{quien}\n\n"
        "Entrá al panel de Admin para activarle la suscripción."
    )
    telegram.enviar_mensaje(texto)

    return {"ok": True}


@router.post("/mencion-chat")
async def mencion_chat(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    Lo llama el trigger notificar_mencion_chat() de Postgres (ver
    sql/021_notificaciones_push.sql), apenas se inserta un mensaje de
    chat con el array `menciones` no vacío — con el texto del mensaje
    y el nombre del autor ya resueltos ahí mismo, para no tener que
    leer nada más por acá.

    Igual que /webhooks/nuevo-registro: quien llama no es un usuario
    logueado de la app (es Postgres), así que no hay Authorization:
    Bearer — el secreto va en un header propio.
    """
    if not settings.MENCION_WEBHOOK_SECRET or x_webhook_secret != settings.MENCION_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secreto inválido")

    try:
        body = await request.json()
    except Exception:
        body = {}

    menciones = body.get("menciones") or []
    if not menciones:
        return {"ok": True, "enviados": 0}

    autor_nombre = body.get("autor_nombre") or "Alguien"
    texto = (body.get("texto") or "").strip()
    preview = texto if len(texto) <= 120 else texto[:117] + "…"

    if not settings.BACKEND_INTERNAL_SECRET:
        logger.error("Falta BACKEND_INTERNAL_SECRET — no se pueden buscar las suscripciones push.")
        return {"ok": False, "enviados": 0}

    cliente = deps.cliente_para_webhook()
    try:
        resp = cliente.rpc(
            "obtener_suscripciones_push_de",
            {"p_secret": settings.BACKEND_INTERNAL_SECRET, "p_usuario_ids": menciones},
        ).execute()
    except Exception:
        logger.exception("No se pudieron buscar las suscripciones push de %s", menciones)
        return {"ok": False, "enviados": 0}

    enviados = 0
    for fila in resp.data or []:
        if push.enviar(
            fila,
            titulo=f"{autor_nombre} te mencionó en el chat",
            cuerpo=preview or "🎵 Audio compartido",
        ):
            enviados += 1

    return {"ok": True, "enviados": enviados}
