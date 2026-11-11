-- ============================================================
-- Catálogo Colaborativo de Remixes — Freemium de catálogo v2 (v12)
-- ============================================================
-- Cambia el freemium de "Cuarteto y Cachengue, sin límite, se puede
-- escuchar" (ver 006_catalogo_freemium.sql) a un esquema de vidriera:
--
--   - Cualquier cuenta logueada (tenga o no suscripción activa) ve
--     TODO el catálogo — título, género, BPM, tonalidad, cantidad de
--     tracks por género, etc. — para que un DJ nuevo vea de entrada
--     todo lo que hay antes de sumarse. Esto es solo METADATA, no el
--     audio en sí.
--   - Sin suscripción activa, lo único que se puede ESCUCHAR (nunca
--     bajar, ni agregar a una playlist) son los hasta 10 tracks de
--     Cachengue subidos más recientemente. Cualquier otra escucha,
--     cualquier descarga y cualquier playlist siguen pidiendo
--     suscripción activa como siempre.
--
-- Importante: con la fila ahora visible para cualquiera, "¿se puede
-- ver por RLS?" deja de servir como proxy de "¿se puede escuchar?"
-- (que es como funcionaba hasta 006). Por eso se agrega
-- puede_escuchar_track(), que es el chequeo real y explícito que
-- ahora usa app/routers/tracks.py:obtener_url_descarga antes de
-- generar una URL de previsualización.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 011_chat_audio.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ---- 1. Visibilidad del catálogo: cualquier cuenta logueada, no
--         solo quien tiene acceso activo o cae en un género
--         "gratuito". admin_ve_todos_los_tracks (ver 002_mejoras.sql)
--         sigue aparte y sigue sumando lo suyo (filas eliminadas,
--         etc.) sin que haga falta tocarla. ----
drop policy if exists "ver_tracks_con_acceso" on public.tracks;
create policy "ver_tracks_con_acceso" on public.tracks
    for select using (
        auth.uid() is not null
        and estado != 'eliminado'
    );

-- ---- 2. ¿Este género es Cachengue (o alguna variante del nombre)?
--         Reemplaza a es_genero_gratuito de 006 — ya no hay un
--         "género gratis entero", así que ese nombre quedaría
--         confuso. Se usa nada más para armar la muestra gratuita de
--         abajo. ----
drop function if exists public.es_genero_gratuito(uuid);

create or replace function public.es_genero_cachengue(p_genero_id uuid)
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select exists (
        select 1 from public.generos
        where id = p_genero_id
        and nombre ilike '%cachengue%'
    );
$$;

comment on function public.es_genero_cachengue is
    'true si el género (por id) es Cachengue o alguna variante del nombre — se usa para armar la muestra gratuita de escucha.';

-- ---- 3. La muestra gratuita en sí: los hasta 10 tracks de Cachengue
--         subidos más recientemente. Una sola función de la que salen
--         tanto el chequeo del backend (puede_escuchar_track, abajo)
--         como la lista que pide el frontend para saber a qué
--         "Reproducir" mostrar activo — así la regla ("¿cuáles son
--         los 10 gratis?") vive en un solo lugar. ----
create or replace function public.obtener_muestra_gratis_cachengue()
returns table (track_id uuid)
language sql
security definer set search_path = public
stable
as $$
    select t.id
    from public.tracks t
    where t.estado = 'activo'
    and public.es_genero_cachengue(t.genero_id)
    order by t.fecha_subida desc
    limit 10;
$$;

comment on function public.obtener_muestra_gratis_cachengue is
    'Ids de los hasta 10 tracks de Cachengue subidos más recientemente — la muestra gratuita de ESCUCHA (no descarga) para quien no tiene suscripción activa.';

grant execute on function public.obtener_muestra_gratis_cachengue() to authenticated;

-- ---- 4. ¿Puede este usuario puntual ESCUCHAR este track puntual?
--         Acceso activo (o admin, que tiene_acceso_activo ya cubre) →
--         sí, cualquiera. Si no, solo si está en la muestra gratuita
--         de arriba. Esto es lo que de verdad decide si se genera una
--         URL firmada de previsualización — ya no alcanza con que la
--         fila sea visible por RLS, porque ahora TODA fila activa es
--         visible para armar el catálogo completo (punto 1). La
--         descarga real sigue sin pasar por acá: esa siempre exige
--         tiene_acceso_activo() aparte, sea cual sea el track. ----
create or replace function public.puede_escuchar_track(p_track_id uuid)
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select public.tiene_acceso_activo()
        or exists (
            select 1 from public.obtener_muestra_gratis_cachengue() m
            where m.track_id = p_track_id
        );
$$;

comment on function public.puede_escuchar_track is
    'true si este usuario puede ESCUCHAR (no implica poder bajar) este track puntual: tiene acceso activo, o el track está en la muestra gratuita de Cachengue.';

grant execute on function public.puede_escuchar_track(uuid) to authenticated;
