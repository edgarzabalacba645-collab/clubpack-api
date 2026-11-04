import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import duplicados, playlists, suscripcion, tracks

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="ClubPack API",
    description=(
        "Backend de ClubPack: todo lo que necesita ffmpeg, librosa, fpcalc "
        "o las claves de Cloudflare R2 — transcodificación, BPM, fingerprint, "
        "armado de ZIPs y escaneo de duplicados. El resto (login, catálogo, "
        "géneros, usuarios) lo maneja el frontend directo contra Supabase."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tracks.router)
app.include_router(duplicados.router)
app.include_router(playlists.router)
app.include_router(suscripcion.router)


@app.get("/health")
def health():
    return {"status": "ok"}
