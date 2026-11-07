from fastapi import APIRouter, Depends

from .. import cloudflare_uso
from ..deps import UsuarioAutenticado, requiere_admin

router = APIRouter(prefix="/admin/uso", tags=["admin-uso"])


@router.get("/cloudflare")
def balance_cloudflare(usuario: UsuarioAutenticado = Depends(requiere_admin)):
    """
    Balance de uso de Cloudflare R2 del mes en curso (storage, Clase A,
    Clase B, costo estimado) — ver cloudflare_uso.py para los detalles
    y los límites gratuitos considerados.
    """
    return cloudflare_uso.obtener_balance()
