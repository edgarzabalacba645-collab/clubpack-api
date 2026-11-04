"""
Dependencias de autenticación/autorización para los endpoints.

Cómo funciona: el frontend (React) hace login directo contra Supabase Auth
y manda el access_token del usuario en cada llamada a esta API
(`Authorization: Bearer <token>`). Acá creamos un cliente de Supabase con
la anon key y le aplicamos ese token al cliente de PostgREST — así todas
las consultas corren autenticadas COMO ESE USUARIO, y las políticas RLS
de sql/schema.sql siguen siendo la única fuente de verdad de qué puede
ver o modificar cada quien. Este backend nunca usa la service_role key.
"""

from dataclasses import dataclass

from fastapi import Header, HTTPException, Depends
from supabase import create_client, Client

from .config import settings


@dataclass
class UsuarioAutenticado:
    client: Client
    user_id: str
    access_token: str


def _crear_cliente_base() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)


def get_usuario_actual(authorization: str | None = Header(default=None)) -> UsuarioAutenticado:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Falta el header Authorization: Bearer <token>")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token vacío")

    client = _crear_cliente_base()

    # Valida el token contra Supabase Auth y de paso obtiene el user id.
    try:
        respuesta = client.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido o vencido")

    if respuesta is None or respuesta.user is None:
        raise HTTPException(status_code=401, detail="Token inválido o vencido")

    # A partir de acá, toda consulta por este client corre como este usuario (RLS).
    client.postgrest.auth(token)

    return UsuarioAutenticado(client=client, user_id=respuesta.user.id, access_token=token)


def requiere_acceso_activo(
    usuario: UsuarioAutenticado = Depends(get_usuario_actual),
) -> UsuarioAutenticado:
    """Exige suscripción activa (o rol admin) — mismo criterio que tiene_acceso_activo() en SQL."""
    resp = usuario.client.rpc("tiene_acceso_activo").execute()
    if not resp.data:
        raise HTTPException(status_code=403, detail="Tu suscripción no está activa. Hablá con un admin del grupo.")
    return usuario


def requiere_admin(
    usuario: UsuarioAutenticado = Depends(get_usuario_actual),
) -> UsuarioAutenticado:
    resp = usuario.client.rpc("es_admin").execute()
    if not resp.data:
        raise HTTPException(status_code=403, detail="Esta acción es solo para administradores.")
    return usuario


def cliente_para_webhook() -> Client:
    """
    Cliente de Supabase SIN JWT de usuario — lo usa el webhook de Mercado
    Pago, que no tiene ninguna sesión de la app (MP no es un usuario
    logueado). Con la anon key sola, este cliente no puede leer ni escribir
    nada protegido por RLS; lo único que puede hacer es llamar a la función
    `procesar_notificacion_mp` (sql/003_pagos.sql), que exige su propio
    secreto interno (BACKEND_INTERNAL_SECRET) antes de escribir algo — ese
    secreto, no este cliente, es lo que realmente protege la operación.
    """
    return _crear_cliente_base()
