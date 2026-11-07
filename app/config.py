"""
Configuración del backend, leída de variables de entorno (.env en local,
variables de entorno reales en el servidor de producción).

Importante: acá SÍ viven las claves sensibles (R2 secret, Supabase anon key
para validar tokens). Este proceso corre en un servidor propio, nunca en
el navegador — por eso puede tener estas claves que el frontend no debe
tener jamás (la service_role key de Supabase tampoco se usa acá: cada
operación de base de datos corre con el token del usuario que llama, para
que las políticas RLS sigan siendo la única fuente de verdad de permisos).
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _env(nombre: str, default: str | None = None, requerido: bool = False) -> str:
    valor = os.environ.get(nombre, default)
    if requerido and not valor:
        raise RuntimeError(f"Falta la variable de entorno {nombre} (ver .env.example)")
    return valor or ""


class Settings:
    SUPABASE_URL = _env("SUPABASE_URL", requerido=True)
    SUPABASE_ANON_KEY = _env("SUPABASE_ANON_KEY", requerido=True)

    R2_ENDPOINT_URL = _env("R2_ENDPOINT_URL", requerido=True)
    R2_ACCESS_KEY_ID = _env("R2_ACCESS_KEY_ID", requerido=True)
    R2_SECRET_ACCESS_KEY = _env("R2_SECRET_ACCESS_KEY", requerido=True)
    R2_BUCKET_NAME = _env("R2_BUCKET_NAME", requerido=True)

    # Orígenes del frontend que pueden llamar a esta API (separados por coma).
    ALLOWED_ORIGINS = [
        o.strip() for o in _env("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]

    # Cuánto tiempo queda disponible un archivo ya analizado/transcodificado
    # esperando la confirmación de subida, antes de descartarse (ver
    # app/staging.py — el archivo en sí vive en disco, no en RAM, así que
    # extender esto no vuelve a traer el problema de memoria de sesiones
    # largas; solo ocupa disco un rato más).
    STAGING_TTL_SEG = int(_env("STAGING_TTL_SEG", "5400"))  # 90 min

    # Tamaño máximo de archivo de audio aceptado en /tracks/analyze.
    MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "200"))

    # --- Mercado Pago (suscripción mensual) ---
    # Todas opcionales: si no están configuradas, /suscripcion/mercadopago/*
    # devuelve un error claro en vez de romper el resto de la API. El grupo
    # puede arrancar solo con transferencia manual y activar Mercado Pago
    # después sin tocar código.
    MP_ACCESS_TOKEN = _env("MP_ACCESS_TOKEN")  # Access Token de PRODUCCIÓN o de TEST, según el momento
    MP_WEBHOOK_SECRET = _env("MP_WEBHOOK_SECRET")  # "Clave secreta" del panel Webhooks de Mercado Pago

    # Secreto compartido solo con la función SQL procesar_notificacion_mp
    # (ver sql/003_pagos.sql) — generado con `openssl rand -hex 32`, NO es
    # ninguna clave de Mercado Pago ni de Supabase.
    BACKEND_INTERNAL_SECRET = _env("BACKEND_INTERNAL_SECRET")

    # Adónde vuelve el usuario en el frontend después de autorizar el pago.
    FRONTEND_URL = _env("FRONTEND_URL", "http://localhost:5173")

    # --- Telegram: alerta instantánea de nuevo registro ---
    # Opcionales: si faltan, el endpoint de webhook responde ok pero no
    # manda nada (no rompe el registro de nadie por esto).
    TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")  # el que da @BotFather
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")  # el que da @userinfobot

    # Secreto compartido solo con el Database Webhook de Supabase que
    # dispara /webhooks/nuevo-registro en cada alta de perfil — generado
    # con `openssl rand -hex 32`, no es ninguna clave de Supabase.
    REGISTRO_WEBHOOK_SECRET = _env("REGISTRO_WEBHOOK_SECRET")

    # Tamaño máximo de imagen aceptada (portadas de track, herramientas DJ).
    MAX_IMAGEN_MB = int(_env("MAX_IMAGEN_MB", "8"))


settings = Settings()
