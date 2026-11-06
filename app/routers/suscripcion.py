import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import deps, mercadopago, telegram
from ..config import settings
from ..deps import UsuarioAutenticado, get_usuario_actual
from ..schemas import AvisoPagoManualRequest, CrearSuscripcionMPResponse

router = APIRouter(prefix="/suscripcion", tags=["suscripcion"])
logger = logging.getLogger("clubpack.suscripcion")

# Mapea el status de un preapproval de Mercado Pago a estado_suscripcion.
_ESTADO_MP_A_INTERNO = {
    "authorized": "activo",
    "paused": "vencido",
    "cancelled": "cancelado",
    "pending": "pendiente",
}


@router.post("/mercadopago/crear", response_model=CrearSuscripcionMPResponse)
def crear_suscripcion_mp(usuario: UsuarioAutenticado = Depends(get_usuario_actual)):
    """
    Crea la suscripción recurrente en Mercado Pago para el usuario logueado
    y devuelve el link de autorización (`init_point`), que el frontend usa
    tanto para el botón "Pagar con Mercado Pago" como para armar el QR.

    A propósito NO exige suscripción activa (`requiere_acceso_activo`): un
    usuario sin acceso es justo quien necesita este endpoint para pagar.
    """
    config_resp = (
        usuario.client.table("configuracion_suscripcion").select("*").limit(1).execute()
    )
    config = (config_resp.data or [None])[0]
    if not config:
        raise HTTPException(status_code=500, detail="No hay configuración de suscripción cargada todavía.")
    if not config.get("mp_habilitado"):
        raise HTTPException(
            status_code=400,
            detail="El pago con Mercado Pago no está habilitado. Usá transferencia manual o avisale a un admin.",
        )
    precio = config.get("precio_mensual") or 0
    if precio <= 0:
        raise HTTPException(status_code=400, detail="Todavía no se configuró el precio de la suscripción.")

    perfil_resp = (
        usuario.client.table("perfiles").select("email, nombre_dj").eq("id", usuario.user_id).single().execute()
    )
    perfil = perfil_resp.data or {}
    email = perfil.get("email")

    try:
        resultado = mercadopago.crear_preapproval(
            usuario_id=usuario.user_id,
            email=email,
            monto=float(precio),
            moneda=config.get("moneda") or "ARS",
            razon=f"Suscripción mensual ClubPack — {perfil.get('nombre_dj') or email}",
        )
    except mercadopago.MercadoPagoError as e:
        logger.exception("Error creando preapproval en Mercado Pago")
        raise HTTPException(status_code=502, detail=str(e))

    return CrearSuscripcionMPResponse(
        preapproval_id=resultado["id"],
        init_point=resultado["init_point"],
        sandbox_init_point=resultado.get("sandbox_init_point"),
    )


@router.post("/aviso-manual")
def aviso_pago_manual(
    body: AvisoPagoManualRequest,
    usuario: UsuarioAutenticado = Depends(get_usuario_actual),
):
    """
    Lo llama el frontend justo después de insertar en `pagos` (tipo
    "manual", vía RLS directo a Supabase — ese insert no pasa por acá).
    Este endpoint no escribe nada: solo manda el aviso a Telegram, mismo
    mecanismo que /webhooks/nuevo-registro, para que el admin se entere al
    toque sin tener que mirar el panel de Admin a cada rato. Si Telegram no
    está configurado o falla, no rompe nada — el aviso ya quedó guardado en
    `pagos` igual, el admin lo va a ver tarde o temprano en Admin.
    """
    perfil_resp = (
        usuario.client.table("perfiles").select("email, nombre_dj").eq("id", usuario.user_id).single().execute()
    )
    perfil = perfil_resp.data or {}
    email = perfil.get("email") or "(sin email)"
    nombre_dj = perfil.get("nombre_dj")
    quien = f"{nombre_dj} ({email})" if nombre_dj and nombre_dj != email else email

    texto = f"💸 <b>Aviso de transferencia en ClubPack</b>\n{quien}"
    if body.comentario:
        texto += f"\n\n{body.comentario}"
    texto += "\n\nEntrá al panel de Admin, pestaña Pagos, para revisarlo y activarle la suscripción."

    telegram.enviar_mensaje(texto)
    return {"ok": True}


@router.post("/mercadopago/webhook")
async def webhook_mercadopago(request: Request):
    """
    Recibe las notificaciones de Mercado Pago (configurado en Tus
    integraciones > Webhooks, apuntando a esta URL pública). No requiere
    login — MP no tiene sesión de la app — así que la única protección es
    validar la firma `x-signature` contra MP_WEBHOOK_SECRET antes de
    confiar en nada del body.

    Tiene que responder 200 en menos de 22 segundos o Mercado Pago reintenta
    cada 15 min — por eso todo lo que hace acá son un par de llamadas HTTP
    rápidas (consultar el recurso en la API de MP) + un rpc a Supabase.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    tipo = (
        body.get("type")
        or body.get("topic")
        or request.query_params.get("type")
        or request.query_params.get("topic")
        or ""
    ).strip()

    data_id = (
        (body.get("data") or {}).get("id")
        or request.query_params.get("data.id")
        or request.query_params.get("id")
    )

    if not data_id:
        # Notificación sin id útil (p.ej. un ping de prueba del panel) — nada para procesar.
        return {"ok": True}

    data_id = str(data_id)
    x_signature = request.headers.get("x-signature")
    x_request_id = request.headers.get("x-request-id")

    if not mercadopago.validar_firma_webhook(
        x_signature=x_signature, x_request_id=x_request_id, data_id=data_id
    ):
        logger.warning("Webhook de Mercado Pago con firma inválida (type=%s, data.id=%s)", tipo, data_id)
        raise HTTPException(status_code=401, detail="Firma inválida")

    if not settings.BACKEND_INTERNAL_SECRET:
        logger.error("Falta BACKEND_INTERNAL_SECRET en el .env — no se puede procesar el webhook.")
        raise HTTPException(status_code=500, detail="Backend mal configurado (falta BACKEND_INTERNAL_SECRET)")

    cliente = deps.cliente_para_webhook()

    try:
        if tipo in ("subscription_preapproval", "preapproval"):
            info = mercadopago.obtener_preapproval(data_id)
            usuario_id = info.get("external_reference")
            estado_suscripcion = _ESTADO_MP_A_INTERNO.get(info.get("status"), "pendiente")

            if not usuario_id:
                logger.warning("Preapproval %s sin external_reference, no se pudo activar a nadie.", data_id)
            else:
                cliente.rpc(
                    "procesar_notificacion_mp",
                    {
                        "p_secret": settings.BACKEND_INTERNAL_SECRET,
                        "p_usuario_id": usuario_id,
                        "p_mp_subscription_id": data_id,
                        "p_estado_suscripcion": estado_suscripcion,
                    },
                ).execute()

        elif tipo in ("subscription_authorized_payment", "payment"):
            pago = mercadopago.obtener_payment(data_id)
            if pago is not None:
                cliente.rpc(
                    "procesar_notificacion_mp",
                    {
                        "p_secret": settings.BACKEND_INTERNAL_SECRET,
                        "p_usuario_id": pago.get("external_reference"),
                        "p_mp_payment_id": data_id,
                        "p_estado_pago": pago.get("status"),
                        "p_monto": pago.get("transaction_amount"),
                        "p_moneda": pago.get("currency_id") or "ARS",
                        "p_payload": pago,
                    },
                ).execute()
        else:
            logger.info("Webhook de Mercado Pago ignorado (type=%s no es de suscripción ni de pago).", tipo)

    except mercadopago.MercadoPagoError:
        logger.exception("Error consultando la API de Mercado Pago para data.id=%s", data_id)
        raise HTTPException(status_code=502, detail="No se pudo consultar Mercado Pago")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error procesando el webhook de Mercado Pago (type=%s, data.id=%s)", tipo, data_id)
        raise HTTPException(status_code=500, detail="Error interno procesando el webhook")

    return {"ok": True}
