-- ============================================================
-- Catálogo Colaborativo de Remixes — Fix: favoritos rompía el catálogo (v16)
-- ============================================================
-- Síntoma, apenas corrida la 015: el catálogo mostraba
--   "Could not embed because more than one relationship was found for
--    'tracks' and 'perfiles'"
-- y ninguna lista cargaba.
--
-- Causa: la tabla favoritos de la 015 tenía clave primaria compuesta
-- (usuario_id, track_id). PostgREST (la API de Supabase) detecta como
-- "tabla intermedia" de una relación muchos-a-muchos a cualquier tabla
-- cuya clave primaria esté formada por claves foráneas a otras dos
-- tablas — así que favoritos pasó a ser, para la API, un segundo camino
-- entre tracks y perfiles, además del directo tracks.subido_por. Con dos
-- caminos, el `perfiles(nombre_dj, email)` que usa el catálogo quedó
-- ambiguo y la API lo rechaza. descargas y reproducciones nunca
-- provocaron esto porque tienen su propio `id` como clave primaria.
--
-- Arreglo: favoritos pasa a tener la misma forma que descargas — un `id`
-- propio como clave primaria y un índice ÚNICO (usuario_id, track_id)
-- que sigue impidiendo marcar dos veces el mismo tema. Sin claves
-- foráneas en la clave primaria, deja de ser tabla intermedia y el
-- embed vuelve a ser único. Nada más cambia: datos, políticas y la
-- función obtener_metricas_tracks() siguen iguales.
--
-- Es idempotente: se puede correr aunque ya se haya corregido.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

do $$
begin
    -- 1. Columna id propia (si todavía no está).
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'favoritos' and column_name = 'id'
    ) then
        alter table public.favoritos add column id uuid not null default gen_random_uuid();
    end if;

    -- 2. Si la clave primaria actual es la compuesta, reemplazarla por id.
    if exists (
        select 1
        from pg_constraint c
        where c.conrelid = 'public.favoritos'::regclass
          and c.contype = 'p'
          and array_length(c.conkey, 1) > 1
    ) then
        alter table public.favoritos drop constraint favoritos_pkey;
    end if;

    if not exists (
        select 1 from pg_constraint c
        where c.conrelid = 'public.favoritos'::regclass and c.contype = 'p'
    ) then
        alter table public.favoritos add constraint favoritos_pkey primary key (id);
    end if;
end;
$$;

-- 3. Un corazón por usuario y tema, igual que antes, ahora como índice único.
create unique index if not exists idx_favoritos_usuario_track_unico
    on public.favoritos (usuario_id, track_id);

-- Avisarle a PostgREST que el esquema cambió (Supabase lo hace solo al
-- rato; esto lo apura).
notify pgrst, 'reload schema';
