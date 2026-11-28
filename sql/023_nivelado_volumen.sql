-- ============================================================
-- Nivelado de volumen (v23)
-- ============================================================
-- Por qué: hasta ahora cada tema quedaba con el volumen que traía el
-- archivo original — una sesión grabada bajo sonaba muy por detrás de
-- un tema bien masterizado en la misma selección de DJ, sin forma de
-- saber de antemano cuáles iban a sonar flojos. Ahora
-- transcodificar_a_mp3_320 (ver app/audio.py) mide la sonoridad real
-- del audio (filtro loudnorm de ffmpeg) y lo nivela a un estándar común
-- (-14 LUFS integrado, el mismo que usan Spotify/YouTube Music) con un
-- único corrimiento de ganancia, sin comprimirle la dinámica al tema.
--
-- Esta columna guarda el volumen (LUFS) al que quedó nivelado un track,
-- y por eso mismo funciona como marca de "ya procesado": NULL = todavía
-- no se le aplicó el nivelado (temas de antes de este cambio, pendientes
-- del backfill de admin), no NULL = ya nivelado. Se completa sola en
-- /tracks/confirm para los temas nuevos, y con el backfill puntual
-- (POST /tracks/{id}/nivelar-volumen) para los que ya estaban subidos —
-- mismo patrón que 008_peaks_waveform.sql para los peaks de waveform.
--
-- A diferencia del backfill de peaks/portada, nivelar el volumen de un
-- track ya subido SÍ reencodea el audio entero (no solo mueve bytes o
-- reescribe metadata) — para MP3, una segunda pasada de lossy a 320kbps
-- es una pérdida muy menor, normalmente inaudible.
--
-- Seguro de correr sobre una base que ya tenga schema.sql ..
-- 022_precios_por_licencia.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

alter table public.tracks
    add column if not exists loudness_lufs numeric(5,1);

comment on column public.tracks.loudness_lufs is
    'Sonoridad integrada (LUFS) a la que quedó nivelado el audio de este track (ver audio.LOUDNORM_I, hoy -14.0). NULL = todavía no se le aplicó el nivelado de volumen (temas de antes de este cambio, pendientes de backfill desde Admin). Ver app/audio.py::transcodificar_a_mp3_320.';
