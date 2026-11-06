"""
Endpoints llamados por servicios externos, SIN JWT de usuario (no hay
sesión de la app corriendo). Se protegen con un secreto compartido en
un header propio — nunca con la service_role key de Supabase.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .. import telegram
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
        f"🆕 <b>Nuevo registro en ClubPack</b>\n{quien}\n\n"
        "Entrá al panel de Admin para activarle la suscripción."
    )
    telegram.enviar_mensaje(texto)

    return {"ok": True}
