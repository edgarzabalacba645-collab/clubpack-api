from fastapi import APIRouter, Depends, HTTPException

from .. import duplicados as dup
from ..deps import UsuarioAutenticado, requiere_admin
from ..schemas import EscaneoResponse, MarcarDuplicadoRequest

router = APIRouter(prefix="/admin/duplicados", tags=["admin-duplicados"])


@router.post("/scan", response_model=EscaneoResponse)
def escanear(usuario: UsuarioAutenticado = Depends(requiere_admin)):
    """
    Recorre todo el catálogo buscando posibles duplicados (fingerprint
    exacto + texto similar) y guarda los hallazgos nuevos. Puede tardar
    unos segundos en catálogos grandes.
    """
    return dup.escanear_catalogo_completo(usuario.client)


@router.get("")
def listar_pendientes(usuario: UsuarioAutenticado = Depends(requiere_admin)):
    return dup.listar_duplicados_pendientes(usuario.client)


@router.patch("/{dup_id}")
def marcar(
    dup_id: str,
    body: MarcarDuplicadoRequest,
    usuario: UsuarioAutenticado = Depends(requiere_admin),
):
    dup.marcar_duplicado(usuario.client, dup_id, body.estado)
    return {"ok": True}
