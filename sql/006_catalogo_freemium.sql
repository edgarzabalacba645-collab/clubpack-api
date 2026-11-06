-- ============================================================
-- Catálogo Colaborativo de Remixes — Freemium de catálogo (v6)
-- ============================================================
-- Deja escuchar gratis (sin suscripción activa) los tracks de los
-- géneros "de prueba" — Cuarteto (y sus variantes: Cuarteto Clásico,
-- Cuarteto Remix, etc.) y Cachengue — para que los DJs nuevos puedan
-- probar el catálogo antes de sumarse. El resto del catálogo y
-- cualquier descarga (de cualquier género, incluidos estos dos) siguen
-- exigiendo suscripción activa como siempre — ese chequeo lo hace el
-- backend en app/routers/tracks.py (obtener_url_descarga) además de
-- esto, porque RLS filtra qué SE VE pero no distingue "escuchar" de
-- "descargar" dentro de un mismo track visible.
--
-- Es seguro correr esto sobre una base que ya tenga schema.sql,
-- 002_mejoras.sql, 003_pagos.sql, 004_fix_fingerprint_index.sql y
-- 005_features.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- Función helper: ¿este género es de los "de prueba gratuita"?
-- Por nombre (ilike) y no por id fijo, para no quedar pisado si en el
-- futuro se agrega otra variante de Cuarteto o se renombra Cachengue.
create or replace function public.es_genero_gratuito(p_genero_id uuid)
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select exists (
        select 1 from public.generos
        where id = p_genero_id
        and (nombre ilike '%cuarteto%' or nombre ilike '%cachengue%')
    );
$$;

comment on function public.es_genero_gratuito is
    'true si el género (por id) es Cuarteto/Cachengue (o variantes) — los géneros de acceso libre sin suscripción.';

-- ---- Tracks: sumar los de género gratuito a lo que ya se podía ver ----
drop policy if exists "ver_tracks_con_acceso" on public.tracks;
create policy "ver_tracks_con_acceso" on public.tracks
    for select using (
        estado != 'eliminado'
        and (public.tiene_acceso_activo() or public.es_genero_gratuito(genero_id))
    );

-- ---- Géneros: la lista en sí no es información sensible — que la vea
--      cualquier usuario logueado (sin esto, alguien sin suscripción no
--      podría ni filtrar/ver qué géneros existen en el catálogo). ----
drop policy if exists "ver_generos_con_acceso" on public.generos;
create policy "ver_generos_con_acceso" on public.generos
    for select using (auth.uid() is not null);
