-- ============================================================
-- Catálogo Colaborativo de Remixes — Tanda de funciones nuevas (v5)
-- ============================================================
-- Agrega: portada por track, chat de comunidad, herramientas DJ
-- descargables, y un endurecimiento de seguridad en perfiles
-- (necesario porque a partir de ahora el usuario puede editar su
-- propia fila para ponerse un nombre de DJ).
--
-- Es seguro correr esto sobre una base que ya tenga schema.sql,
-- 002_mejoras.sql y 003_pagos.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ------------------------------------------------------------
-- 1. PORTADA POR TRACK
-- ------------------------------------------------------------
alter table public.tracks
    add column if not exists portada_url text;

comment on column public.tracks.portada_url is
    'Key del objeto en Cloudflare R2 (prefijo "portadas/") con la carátula del track. NULL = el frontend muestra la genérica de ClubPack.';


-- ------------------------------------------------------------
-- 1.b CHEQUEO DE FINGERPRINT AL ANALIZAR (no solo en el escaneo manual)
-- ------------------------------------------------------------
-- El fingerprint de Chromaprint puede ser larguísimo (por eso ya hubo
-- que indexar su MD5 en vez del valor crudo, ver 002_mejoras.sql). Por
-- la misma razón, comparar por el fingerprint crudo en una consulta de
-- la API (PostgREST manda los filtros por query string) podría generar
-- una URL enorme en temas largos. Se agrega una columna generada con
-- el MD5 (corto, siempre 32 caracteres) para que el backend filtre por
-- ahí — ver duplicados.chequear_fingerprint_exacto().
alter table public.tracks
    add column if not exists audio_fingerprint_md5 text
        generated always as (md5(audio_fingerprint)) stored;

-- El índice funcional de 002_mejoras.sql queda obsoleto: ahora hay una
-- columna real para eso, que además es la que arma su propio índice.
drop index if exists public.idx_tracks_fingerprint_md5;
create index if not exists idx_tracks_fingerprint_md5 on public.tracks (audio_fingerprint_md5);


-- ------------------------------------------------------------
-- 2. CHAT DE COMUNIDAD
-- ------------------------------------------------------------
create table if not exists public.mensajes_chat (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid not null references public.perfiles(id) on delete cascade,
    mensaje text not null check (char_length(trim(mensaje)) > 0 and char_length(mensaje) <= 1000),
    fecha timestamptz not null default now()
);

comment on table public.mensajes_chat is 'Chat único del grupo (sin salas) — para pedir temas/versiones o charlar';

create index if not exists idx_mensajes_chat_fecha on public.mensajes_chat (fecha desc);

alter table public.mensajes_chat enable row level security;

drop policy if exists "ver_chat_con_acceso" on public.mensajes_chat;
create policy "ver_chat_con_acceso" on public.mensajes_chat
    for select using (public.tiene_acceso_activo());

drop policy if exists "escribir_chat_propio" on public.mensajes_chat;
create policy "escribir_chat_propio" on public.mensajes_chat
    for insert with check (public.tiene_acceso_activo() and usuario_id = auth.uid());

drop policy if exists "borrar_mensaje_propio_o_admin" on public.mensajes_chat;
create policy "borrar_mensaje_propio_o_admin" on public.mensajes_chat
    for delete using (usuario_id = auth.uid() or public.es_admin());

-- Habilita Realtime (INSERT/DELETE en vivo) para esta tabla puntual.
-- Si ya estuviera agregada, Postgres tira error "relation is already
-- member" — por eso va en un bloque que lo ignora.
do $$
begin
    alter publication supabase_realtime add table public.mensajes_chat;
exception
    when duplicate_object then null;
end $$;


-- ------------------------------------------------------------
-- 3. HERRAMIENTAS DJ (descargables, solo el admin las sube)
-- ------------------------------------------------------------
create table if not exists public.herramientas_dj (
    id uuid primary key default gen_random_uuid(),
    nombre text not null,
    descripcion text,
    imagen_path text,          -- key en R2 (prefijo "herramientas/"), opcional
    link_descarga text not null,
    creado_por uuid references public.perfiles(id) on delete set null,
    orden integer not null default 0,
    fecha_creacion timestamptz not null default now()
);

comment on table public.herramientas_dj is 'Herramientas para DJ (plugins, packs, etc.) que cuelga el admin con imagen + link de descarga';

alter table public.herramientas_dj enable row level security;

drop policy if exists "ver_herramientas_con_acceso" on public.herramientas_dj;
create policy "ver_herramientas_con_acceso" on public.herramientas_dj
    for select using (public.tiene_acceso_activo());

drop policy if exists "admin_gestiona_herramientas" on public.herramientas_dj;
create policy "admin_gestiona_herramientas" on public.herramientas_dj
    for all using (public.es_admin()) with check (public.es_admin());


-- ------------------------------------------------------------
-- 4. SEGURIDAD: ahora que el usuario edita su propia fila de perfil
--    (para ponerse un nombre de DJ), hay que impedir que esa misma
--    puerta sirva para auto-asignarse rol=admin, activarse la
--    suscripción o cambiarse el email a mano, con un simple PATCH
--    directo a la API de Supabase (sin pasar por la UI).
--
--    En vez de restringir la policy de "editar_propio_perfil" (que
--    ya existe desde antes y podría estar sosteniendo otras cosas),
--    se agrega un trigger que, cuando quien edita NO es admin, fija
--    esos campos sensibles al valor que ya tenían — pase lo que pase
--    en el UPDATE recibido. El resto de columnas (como nombre_dj)
--    queda libre para que cada uno edite la suya.
-- ------------------------------------------------------------
create or replace function public.proteger_campos_perfil()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    if not public.es_admin() then
        new.rol := old.rol;
        new.estado_suscripcion := old.estado_suscripcion;
        new.mp_subscription_id := old.mp_subscription_id;
        new.email := old.email;
    end if;
    return new;
end;
$$;

comment on function public.proteger_campos_perfil is
    'Trigger de perfiles: un usuario no-admin puede editar su propia fila (p.ej. nombre_dj), pero no puede tocar rol/estado_suscripcion/mp_subscription_id/email aunque los mande en el UPDATE.';

drop trigger if exists proteger_campos_perfil_trigger on public.perfiles;
create trigger proteger_campos_perfil_trigger
    before update on public.perfiles
    for each row execute function public.proteger_campos_perfil();
