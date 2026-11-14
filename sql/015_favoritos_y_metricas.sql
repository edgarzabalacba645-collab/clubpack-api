-- ============================================================
-- Catálogo Colaborativo de Remixes — Favoritos y métricas por track (v15)
-- ============================================================
-- Dos cosas, para el rediseño del catálogo:
--
--   1. Favoritos: cada usuario puede marcar con un corazón los temas que
--      quiera. El CONTEO de corazones de cada tema lo ve todo el grupo
--      (al lado del corazón, en la portada y en la fila), y la página
--      "Favoritos" lista solo los temas con 1+ corazón, de mayor a menor.
--      Quién marcó qué sigue siendo privado: cada uno ve únicamente sus
--      propias filas (para pintar su corazón), y el total sale agregado
--      por la función de abajo.
--
--   2. obtener_metricas_tracks(): descargas, reproducciones y favoritos
--      por track, agregado, en una sola llamada — para el "Ordenar por
--      más descargados / más escuchados / más favoritos" del catálogo y
--      para los contadores. Mismo criterio que obtener_ranking_top_50
--      (007): se expone el número por tema, nunca quién ni cuándo.
--
-- Cualquier cuenta logueada puede marcar favoritos y ver los conteos:
-- desde 012 el catálogo entero es visible como vidriera, y un corazón
-- no da acceso a nada (escuchar/bajar siguen con sus chequeos propios).
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 014_actividad_por_usuario.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ---- 1. Tabla favoritos ----
-- OJO: con `id` propio como clave primaria y un índice ÚNICO
-- (usuario_id, track_id) — NO una clave primaria compuesta por las dos
-- FKs. PostgREST toma a cualquier tabla cuya clave primaria sean FKs a
-- otras dos tablas como "tabla intermedia" muchos-a-muchos, y eso
-- convertía a favoritos en un segundo camino tracks↔perfiles: el embed
-- perfiles(...) del catálogo quedaba ambiguo y la API lo rechazaba
-- ("more than one relationship was found"). Ver 016 para el arreglo
-- sobre una base que corrió la primera versión de este archivo.
create table if not exists public.favoritos (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid not null references public.perfiles(id) on delete cascade,
    track_id uuid not null references public.tracks(id) on delete cascade,
    fecha timestamptz not null default now()
);

create unique index if not exists idx_favoritos_usuario_track_unico
    on public.favoritos (usuario_id, track_id);
create index if not exists idx_favoritos_track on public.favoritos (track_id);

comment on table public.favoritos is
    'Corazones: qué temas marcó como favorito cada usuario. El conteo por tema lo ve todo el grupo vía obtener_metricas_tracks(); las filas, solo su dueño.';

alter table public.favoritos enable row level security;

drop policy if exists "ver_favoritos_propios" on public.favoritos;
create policy "ver_favoritos_propios" on public.favoritos
    for select using (usuario_id = auth.uid());

drop policy if exists "marcar_favorito_propio" on public.favoritos;
create policy "marcar_favorito_propio" on public.favoritos
    for insert with check (usuario_id = auth.uid());

drop policy if exists "quitar_favorito_propio" on public.favoritos;
create policy "quitar_favorito_propio" on public.favoritos
    for delete using (usuario_id = auth.uid());

-- ---- 2. Métricas agregadas por track (cualquier cuenta logueada) ----
-- Devuelve solo los tracks activos que tienen al menos un número
-- distinto de cero — el front toma "no está" como 0 en todo, así el
-- payload no crece con cada tema nuevo que todavía nadie tocó.
create or replace function public.obtener_metricas_tracks()
returns table (
    track_id uuid,
    descargas bigint,
    reproducciones bigint,
    favoritos bigint
)
language sql
security definer set search_path = public
stable
as $$
    select
        t.id,
        coalesce(d.n, 0)::bigint,
        coalesce(r.n, 0)::bigint,
        coalesce(f.n, 0)::bigint
    from public.tracks t
    left join (select track_id, count(*) as n from public.descargas group by track_id) d
        on d.track_id = t.id
    left join (select track_id, count(*) as n from public.reproducciones group by track_id) r
        on r.track_id = t.id
    left join (select track_id, count(*) as n from public.favoritos group by track_id) f
        on f.track_id = t.id
    where auth.uid() is not null
      and t.estado = 'activo'
      and (coalesce(d.n, 0) + coalesce(r.n, 0) + coalesce(f.n, 0)) > 0;
$$;

comment on function public.obtener_metricas_tracks is
    'Descargas, reproducciones y favoritos por track activo (solo los que tienen algo), agregado — sin exponer quién ni cuándo. Vacío sin sesión.';

grant execute on function public.obtener_metricas_tracks() to authenticated;
