from typing import Literal

from pydantic import BaseModel, Field


class DuplicadoCandidato(BaseModel):
    id: str
    titulo: str
    remixer: str | None = None
    similitud: float


class AnalisisTrackResponse(BaseModel):
    staging_id: str
    nombre_original: str
    tamano_mb: float
    duracion_seg: int | None = None
    bpm_detectado: float | None = None
    key_detectada: str | None = None
    fingerprint: str | None = None
    advertencias: list[str] = Field(default_factory=list)
    duplicados_sospechosos: list[DuplicadoCandidato] = Field(default_factory=list)
    # True si el archivo trae carátula embebida (tag ID3 APIC) — el
    # frontend solo necesita mostrar la elección "mantener original vs.
    # portada de ClubPack" cuando esto es True; si no hay carátula, va
    # derecho la de ClubPack sin preguntar nada.
    tiene_caratula_original: bool = False
    # La carátula embebida en sí, como data URI (ej. "data:image/jpeg;
    # base64,..."), para que el frontend la pueda mostrar antes de
    # confirmar — sin esto, elegir "mantener la original" a ciegas no
    # tiene sentido. None si no hay carátula.
    caratula_original_preview: str | None = None


class ConfirmarSubidaRequest(BaseModel):
    staging_id: str
    titulo: str
    artista_original: str | None = None
    remixer: str | None = None
    genero_id: str | None = None
    bpm: float | None = None
    key_armonica: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Solo importa cuando el análisis reportó tiene_caratula_original=True.
    # Default True para no cambiar el comportamiento de ningún cliente
    # viejo que no mande este campo.
    usar_caratula_original: bool = True


class TrackOut(BaseModel):
    id: str
    titulo: str
    artista_original: str | None = None
    remixer: str | None = None
    genero: str | None = None
    genero_id: str | None = None
    bpm: float | None = None
    key_armonica: str | None = None
    tags: list[str] = Field(default_factory=list)
    storage_path: str
    duracion_seg: int | None = None
    tamano_mb: float | None = None
    subido_por: str | None = None
    fecha_subida: str
    estado: str
    portada_url: str | None = None


class DescargaUrlResponse(BaseModel):
    url: str
    expira_seg: int


class ZipRequest(BaseModel):
    track_ids: list[str] = Field(min_length=1)
    nombre_zip: str | None = None


class EscaneoResponse(BaseModel):
    tracks_analizados: int
    nuevos_por_fingerprint: int
    nuevos_por_texto: int


class MarcarDuplicadoRequest(BaseModel):
    estado: Literal["descartado", "resuelto"]


class ImagenUrlResponse(BaseModel):
    url: str


class ImagenSubidaResponse(BaseModel):
    path: str


class CrearSuscripcionMPResponse(BaseModel):
    preapproval_id: str
    init_point: str
    sandbox_init_point: str | None = None


class AvisoPagoManualRequest(BaseModel):
    comentario: str | None = None
