import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import audio
from .config import settings
from .routers import chat, duplicados, imagenes, playlists, suscripcion, tracks, uso, webhooks

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def _ciclo_de_vida(app: FastAPI):
    # Ver la docstring de audio.precalentar_librosa(): paga de una vez,
    # acá al arrancar (proceso recién iniciado, sin ningún archivo real
    # en juego), el golpe fijo de ~200-350MB que numba agrega la PRIMERA
    # vez que se usa beat_track/chroma_cqt — en vez de que ese golpe le
    # toque al primer DJ que suba un track, justo mientras su archivo
    # todavía ocupa memoria/disco. Es la causa real por la que sesiones
    # largas (~200MB) seguían fallando con "No se pudo conectar con el
    # servidor" en el plan gratuito de Render (512MB) incluso después de
    # dejar de cargar el archivo entero en memoria de Python.
    audio.precalentar_librosa()
    yield


app = FastAPI(
    title="ClubPack API",
    description=(
        "Backend de ClubPack: todo lo que necesita ffmpeg, librosa, fpcalc "
        "o las claves de Cloudflare R2 — transcodificación, BPM, fingerprint, "
        "armado de ZIPs y escaneo de duplicados. El resto (login, catálogo, "
        "géneros, usuarios) lo maneja el frontend directo contra Supabase."
    ),
    version="0.1.0",
    lifespan=_ciclo_de_vida,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tracks.router)
app.include_router(chat.router)
app.include_router(duplicados.router)
app.include_router(playlists.router)
app.include_router(suscripcion.router)
app.include_router(imagenes.router)
app.include_router(uso.router)
app.include_router(webhooks.router)


@app.get("/health")
def health():
    return {"status": "ok"}
