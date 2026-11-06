-- ============================================================
-- Catálogo Colaborativo de Remixes — Top 50 visible para todos (v7)
-- ============================================================
-- Hasta ahora el ranking de Top 50 vivía como una pestaña adentro de
-- AdminPage, pensado "solo para admins". Pero el problema era más
-- profundo que la ubicación en la UI: esa pestaña leía directo de
-- public.descargas con el cliente de Supabase, y la política RLS
-- "ver_descargas_propias_o_admin" (ver schema.sql) solo deja ver las
-- descargas PROPIAS a quien no es admin — así que aunque se hubiera
-- sacado esa sección de adentro del panel de Admin, a un miembro
-- normal el ranking le habría salido mal (armado solo con sus propias
-- descargas, no las de todo el grupo).
--
-- La solución no es abrir la tabla descargas entera (eso expondría a
-- cada miembro el historial de descargas de todos los demás, con
-- usuario y fecha — más de lo que hace falta para un ranking). En
-- cambio, esta función devuelve solo el conteo ya agregado por track
-- (sin usuario_id ni fecha), corriendo con los permisos del dueño de
-- la función (security definer) para poder sumar las descargas de
-- todo el grupo, pero sin filtrar por quién las hizo en el resultado.
--
-- Sigue pidiendo tiene_acceso_activo() — el mismo gate que ya tienen
-- los tracks del catálogo — así que un miembro sin suscripción activa
-- ve el ranking vacío, igual que pasaría si intentara ver el catálogo
-- completo. "Para todos" en este contexto es "para todos los
-- miembros", no admins-only; no cambia qué nivel de acceso hace falta
-- para usar la app.
--
-- Es seguro correr esto sobre una base que ya tenga schema.sql,
-- 002_mejoras.sql, 003_pagos.sql, 004_fix_fingerprint_index.sql,
-- 005_features.sql y 006_catalogo_freemium.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

create or replace function public.obtener_ranking_top_50()
returns table (
    track_id uuid,
    titulo text,
    remixer text,
    descargas bigint
)
language plpgsql
security definer set search_path = public
stable
as $$
begin
    -- Mismo gate que ver_tracks_con_acceso: sin suscripción activa (ni
    -- ser admin), no hay ranking — devuelve 0 filas en vez de un error,
    -- para que el front lo trate igual que "todavía no hay descargas".
    if not public.tiene_acceso_activo() then
        return;
    end if;

    return query
        select t.id, t.titulo, t.remixer, count(d.id)::bigint as descargas
        from public.descargas d
        join public.tracks t on t.id = d.track_id
        group by t.id, t.titulo, t.remixer
        order by count(d.id) desc, t.titulo asc
        limit 50;
end;
$$;

comment on function public.obtener_ranking_top_50 is
    'Top 50 tracks más descargados, agregado por track (sin exponer qué usuario bajó qué ni cuándo) — para que lo vea cualquier miembro con acceso activo, no solo admins.';

-- RLS sigue protegiendo descargas/tracks como siempre; esto solo le da
-- a cualquier usuario autenticado permiso para EJECUTAR la función
-- (que adentro ya hace su propio chequeo de acceso).
grant execute on function public.obtener_ranking_top_50() to authenticated;
