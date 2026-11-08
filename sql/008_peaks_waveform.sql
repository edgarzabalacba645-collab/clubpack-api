-- ============================================================
-- Peaks de waveform precalculados (v8)
-- ============================================================
-- Por qué: wavesurfer.js, si no le pasamos nosotros los datos de la
-- onda, baja el archivo ENTERO y lo decodifica entero antes de poder
-- arrancar a sonar (lo necesita para dibujar la forma de onda) — eso
-- es lo que hacía que el player tardara en arrancar cada tema,
-- sobre todo en el celular con señal floja. Pasándole un array de
-- "peaks" (picos de amplitud) ya calculado, wavesurfer dibuja la onda
-- al toque y deja que el audio se reproduzca en streaming nativo
-- (progresivo), como cualquier <audio> normal.
--
-- Estos peaks se calculan en el backend (ver app/audio.py::
-- calcular_peaks) en el momento de /tracks/confirm para los temas
-- nuevos, y con un backfill puntual (endpoint de admin) para los
-- temas que ya estaban subidos.
--
-- `real[]` (no jsonb) para que sea consistente con `tags text[]` que
-- ya existe en esta misma tabla — mismo tipo de dato, mismo manejo
-- por el cliente de Supabase. Nullable: un track sin peaks calculados
-- todavía simplemente hace que el player use el camino viejo (bajar+
-- decodificar entero) para ESE tema puntual, sin romper nada.
--
-- Tamaño: ~1200 puntos por tema, no depende de la duración del tema.
-- Con los ~180 temas actuales del catálogo, esto pesa menos de 2MB en
-- total — y vive en la base de Postgres de Supabase, no en R2 (no
-- afecta el panel de "Uso y costos" de Cloudflare).
--
-- Seguro de correr sobre una base que ya tenga schema.sql,
-- 002_mejoras.sql .. 007_top50_para_todos.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

alter table public.tracks
    add column if not exists peaks real[];

comment on column public.tracks.peaks is
    'Envolvente de volumen precalculada (picos de amplitud entre 0 y 1, cantidad fija de puntos sin importar la duración) para que el player arranque a reproducir sin bajar+decodificar el archivo entero primero. NULL = todavía no calculado para este track (usa el camino viejo, más lento, en ese caso). Ver app/audio.py::calcular_peaks.';
