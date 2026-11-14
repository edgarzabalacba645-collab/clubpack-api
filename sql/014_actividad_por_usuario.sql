-- ============================================================
-- Catálogo Colaborativo de Remixes — Actividad por usuario (v14)
-- ============================================================
-- Para la pestaña "Actividad" de Admin: cuánto subió, bajó y escuchó
-- cada usuario, y el detalle de sus últimos movimientos.
--
--   - Subidas: ya estaban (tracks.subido_por).
--   - Descargas: ya estaban (tabla descargas, la llena el backend en
--     cada download-url con registrar=true, /tracks/zip y
--     /playlists/{id}/zip).
--   - Reproducciones: NO existían. Se agrega la tabla reproducciones y
--     la función registrar_reproduccion(), que llama el player del
--     frontend (ver lib/player.tsx) una vez por tema cuando ARRANCA a
--     sonar de verdad — no cuando se pide la URL firmada, porque el
--     player precarga la URL del tema siguiente apenas empieza el
--     actual (y ese siguiente puede no llegar a sonar nunca), así que
--     contar en download-url inflaría el número.
--
-- Lectura: solo admin, y solo a través de las dos funciones de abajo
-- (security definer con chequeo de es_admin() adentro). Los miembros
-- pueden ver sus propias reproducciones por RLS, igual que con
-- descargas — nada más.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 013_borrar_usuarios.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ---- 1. Tabla reproducciones (misma forma que descargas) ----
create table if not exists public.reproducciones (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid not null references public.perfiles(id) on delete cascade,
    track_id uuid not null references public.tracks(id) on delete cascade,
    fecha timestamptz not null default now()
);

create index if not exists idx_reproducciones_usuario on public.reproducciones (usuario_id);
create index if not exists idx_reproducciones_track on public.reproducciones (track_id);
create index if not exists idx_reproducciones_fecha on public.reproducciones (fecha desc);

comment on table public.reproducciones is
    'Log de quién escuchó qué (una fila por vez que un tema arranca a sonar en el player) — para el visor de actividad del admin.';

alter table public.reproducciones enable row level security;

drop policy if exists "ver_reproducciones_propias_o_admin" on public.reproducciones;
create policy "ver_reproducciones_propias_o_admin" on public.reproducciones
    for select using (usuario_id = auth.uid() or public.es_admin());

-- A propósito NO hay policy de insert: la única puerta de entrada es
-- registrar_reproduccion(), que pone auth.uid() ella misma (así nadie
-- puede anotarle reproducciones a otro) y exige poder escuchar el tema.

-- ---- 2. Registrar una reproducción (la llama el player) ----
create or replace function public.registrar_reproduccion(p_track_id uuid)
returns void
language plpgsql
security definer set search_path = public
as $$
begin
    if auth.uid() is null then
        return;
    end if;
    -- Mismo criterio que el backend usa para dar la URL de escucha:
    -- acceso activo, o el tema está en la muestra gratis de Cachengue.
    if not public.puede_escuchar_track(p_track_id) then
        return;
    end if;
    insert into public.reproducciones (usuario_id, track_id)
    values (auth.uid(), p_track_id);
end;
$$;

comment on function public.registrar_reproduccion is
    'Anota una reproducción del tema para el usuario logueado. Silenciosa si no hay sesión o no puede escuchar ese tema.';

grant execute on function public.registrar_reproduccion(uuid) to authenticated;

-- ---- 3. Resumen por usuario (solo admin) ----
create or replace function public.obtener_actividad_por_usuario()
returns table (
    usuario_id uuid,
    nombre_dj text,
    email text,
    rol text,
    estado_suscripcion text,
    fecha_alta timestamptz,
    subidas bigint,
    descargas bigint,
    reproducciones bigint,
    ultima_actividad timestamptz
)
language plpgsql
security definer set search_path = public
stable
as $$
begin
    if not public.es_admin() then
        return;
    end if;

    return query
        select
            p.id,
            p.nombre_dj,
            p.email,
            p.rol,
            p.estado_suscripcion,
            p.fecha_alta,
            (select count(*) from public.tracks t
                where t.subido_por = p.id and t.estado != 'eliminado')::bigint,
            (select count(*) from public.descargas d where d.usuario_id = p.id)::bigint,
            (select count(*) from public.reproducciones r where r.usuario_id = p.id)::bigint,
            -- greatest() ignora los null: si el usuario nunca hizo nada,
            -- queda null y el front muestra "—".
            greatest(
                (select max(t.fecha_subida) from public.tracks t
                    where t.subido_por = p.id and t.estado != 'eliminado'),
                (select max(d.fecha) from public.descargas d where d.usuario_id = p.id),
                (select max(r.fecha) from public.reproducciones r where r.usuario_id = p.id)
            )
        from public.perfiles p
        order by 10 desc nulls last, p.email asc;
end;
$$;

comment on function public.obtener_actividad_por_usuario is
    'Una fila por usuario con cuántos temas subió, bajó y escuchó, y su última actividad. Solo devuelve filas a un admin.';

grant execute on function public.obtener_actividad_por_usuario() to authenticated;

-- ---- 4. Detalle de un usuario: sus últimos movimientos (solo admin) ----
create or replace function public.obtener_detalle_actividad_usuario(
    p_usuario_id uuid,
    p_limite integer default 200
)
returns table (
    tipo text,
    track_id uuid,
    titulo text,
    remixer text,
    fecha timestamptz
)
language plpgsql
security definer set search_path = public
stable
as $$
begin
    if not public.es_admin() then
        return;
    end if;

    return query
        select x.x_tipo, x.x_track_id, x.x_titulo, x.x_remixer, x.x_fecha from (
            select 'subida'::text as x_tipo, t.id as x_track_id, t.titulo as x_titulo,
                   t.remixer as x_remixer, t.fecha_subida as x_fecha
            from public.tracks t
            where t.subido_por = p_usuario_id and t.estado != 'eliminado'
            union all
            select 'descarga'::text, t.id, t.titulo, t.remixer, d.fecha
            from public.descargas d
            join public.tracks t on t.id = d.track_id
            where d.usuario_id = p_usuario_id
            union all
            select 'reproduccion'::text, t.id, t.titulo, t.remixer, r.fecha
            from public.reproducciones r
            join public.tracks t on t.id = r.track_id
            where r.usuario_id = p_usuario_id
        ) x
        order by x.x_fecha desc
        limit p_limite;
end;
$$;

comment on function public.obtener_detalle_actividad_usuario is
    'Últimos movimientos (subidas, descargas y reproducciones, mezclados por fecha) de un usuario puntual. Solo devuelve filas a un admin.';

grant execute on function public.obtener_detalle_actividad_usuario(uuid, integer) to authenticated;
