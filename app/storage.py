"""
Storage de los archivos de audio en Cloudflare R2 (API compatible con S3).

Por qué R2 y no Supabase Storage: R2 no cobra por transferencia de salida
(egress), que es justo lo que más pesa acá (cada descarga de un DJ es
tráfico de salida). Supabase Storage queda solo para la DB y el Auth.

Portado de utils/storage.py del prototipo en Streamlit — misma lógica,
cambiando st.secrets/st.cache_resource por config.py/lru_cache.
"""

import io
import uuid
from functools import lru_cache

import boto3
from botocore.client import Config

from .config import settings


@lru_cache(maxsize=1)
def _cliente_r2():
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def subir_archivo(contenido: bytes, nombre_original: str) -> str:
    """
    Sube un archivo de audio al bucket y devuelve el storage_path
    (la key del objeto) para guardar en la tabla tracks.
    """
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else "mp3"
    key = f"tracks/{uuid.uuid4()}.{extension}"

    cliente = _cliente_r2()
    cliente.upload_fileobj(io.BytesIO(contenido), settings.R2_BUCKET_NAME, key)
    return key


def subir_bytes(contenido: bytes, key: str) -> str:
    """
    Como subir_archivo, pero con la key exacta que se le pida (en vez
    de generar un nombre random) — lo usan las portadas de tracks y
    las imágenes de herramientas DJ, que quieren una key predecible
    (p.ej. "portadas/{track_id}.jpg") para poder reemplazarla después.
    """
    cliente = _cliente_r2()
    cliente.upload_fileobj(io.BytesIO(contenido), settings.R2_BUCKET_NAME, key)
    return key


def generar_url_descarga(storage_path: str, nombre_descarga: str | None = None, expira_seg: int = 300) -> str:
    """
    Genera una URL firmada y temporal (default 5 min) para bajar o
    previsualizar un track. No requiere que el bucket sea público.
    """
    cliente = _cliente_r2()

    params = {"Bucket": settings.R2_BUCKET_NAME, "Key": storage_path}
    if nombre_descarga:
        params["ResponseContentDisposition"] = f'attachment; filename="{nombre_descarga}"'

    return cliente.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expira_seg,
    )


def descargar_bytes(storage_path: str) -> bytes:
    """
    Trae el contenido del archivo directo desde R2 (server-side).
    Se usa para armar ZIPs de varios tracks juntos — para un solo
    track conviene generar_url_descarga en vez de esto.
    """
    cliente = _cliente_r2()
    obj = cliente.get_object(Bucket=settings.R2_BUCKET_NAME, Key=storage_path)
    return obj["Body"].read()


def borrar_archivo(storage_path: str):
    cliente = _cliente_r2()
    cliente.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=storage_path)
