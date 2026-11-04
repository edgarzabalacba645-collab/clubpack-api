-- ============================================================
-- Catálogo Colaborativo de Remixes (DJs) — Schema Supabase
-- ============================================================
-- Ejecutar completo en: Supabase > SQL Editor > New query
-- Requiere: proyecto Supabase con Auth habilitado (email/password)
-- ============================================================

-- ------------------------------------------------------------
-- 1. PERFILES
-- Extiende auth.users con datos propios de la app.
-- Se crea automáticamente vía trigger cuando alguien se registra.
-- ------------------------------------------------------------
create table if not exists public.perfiles (
    id uuid primary key references auth.users(id) on delete cascade,
    nombre_dj text,
    email text,
    rol text not null default 'miembro' check (rol in ('miembro', 'admin')),
    estado_suscripcion text not null default 'pendiente'
        check (estado_suscripcion in ('activo', 'vencido', 'cancelado', 'pendiente')),
    mp_subscription_id text,
    fecha_alta timestamptz not null default now()
);

comment on table public.perfiles is 'Datos de cada usuario del catálogo, 1 a 1 con auth.users';
comment on column public.perfiles.rol is 'admin = fundadores, acceso total siempre. miembro = paga suscripción';
comment on column public.perfiles.estado_suscripcion is 'Se actualiza manualmente al principio; luego vía webhook de Mercado Pago';

-- Trigger: cuando alguien se registra en auth.users, crear su fila en perfiles
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    insert into public.perfiles (id, email, nombre_dj)
    values (new.id, new.email, split_part(new.email, '@', 1));
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();


-- ------------------------------------------------------------
-- 2. TRACKS
-- Cada remix/edit subido al catálogo.
-- ------------------------------------------------------------
create table if not exists public.tracks (
    id uuid primary key default gen_random_uuid(),
    titulo text not null,
    artista_original text,
    remixer text,                      -- quién hizo el remix/edit (DJ o productor)
    genero text,
    bpm numeric(5,1),
    key_armonica text,                 -- notación Camelot, ej: "8A"
    tags text[] default '{}',          -- ej: {edit, mashup, bootleg, extended}
    storage_path text not null,        -- ruta dentro del bucket de R2
    duracion_seg integer,
    tamano_mb numeric(6,2),
    subido_por uuid references public.perfiles(id) on delete set null,
    fecha_subida timestamptz not null default now()
);

create index if not exists idx_tracks_genero on public.tracks (genero);
create index if not exists idx_tracks_bpm on public.tracks (bpm);
create index if not exists idx_tracks_remixer on public.tracks (remixer);
create index if not exists idx_tracks_subido_por on public.tracks (subido_por);

comment on table public.tracks is 'Catálogo de remixes/edits subidos por los miembros';
comment on column public.tracks.storage_path is 'Key del objeto en el bucket de Cloudflare R2, no la URL pública';


-- ------------------------------------------------------------
-- 3. DESCARGAS
-- Log de quién bajó qué, para el Top 50 y soporte.
-- ------------------------------------------------------------
create table if not exists public.descargas (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid not null references public.perfiles(id) on delete cascade,
    track_id uuid not null references public.tracks(id) on delete cascade,
    fecha timestamptz not null default now()
);

create index if not exists idx_descargas_track on public.descargas (track_id);
create index if not exists idx_descargas_usuario on public.descargas (usuario_id);


-- ------------------------------------------------------------
-- 4. PAGOS
-- Log crudo de eventos de Mercado Pago (para debug de webhooks).
-- No se toca manualmente salvo excepción puntual.
-- ------------------------------------------------------------
create table if not exists public.pagos (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid references public.perfiles(id) on delete set null,
    mp_payment_id text,
    estado text,                       -- approved, pending, rejected, cancelled...
    monto numeric(10,2),
    moneda text default 'ARS',
    payload_crudo jsonb,               -- el body completo que mandó MP, por si hay que auditar
    fecha timestamptz not null default now()
);


-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
alter table public.perfiles enable row level security;
alter table public.tracks enable row level security;
alter table public.descargas enable row level security;
alter table public.pagos enable row level security;

-- Función helper: ¿el usuario actual tiene acceso vigente al catálogo?
create or replace function public.tiene_acceso_activo()
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select exists (
        select 1 from public.perfiles
        where id = auth.uid()
        and (estado_suscripcion = 'activo' or rol = 'admin')
    );
$$;

-- Función helper: ¿el usuario actual es admin?
create or replace function public.es_admin()
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select exists (
        select 1 from public.perfiles
        where id = auth.uid() and rol = 'admin'
    );
$$;

-- ---- Políticas: perfiles ----
create policy "ver_propio_perfil" on public.perfiles
    for select using (id = auth.uid() or public.es_admin());

create policy "editar_propio_perfil" on public.perfiles
    for update using (id = auth.uid() or public.es_admin());

-- ---- Políticas: tracks ----
create policy "ver_tracks_con_acceso" on public.tracks
    for select using (public.tiene_acceso_activo());

create policy "subir_tracks_con_acceso" on public.tracks
    for insert with check (public.tiene_acceso_activo() and subido_por = auth.uid());

create policy "editar_tracks_propios_o_admin" on public.tracks
    for update using (subido_por = auth.uid() or public.es_admin());

create policy "borrar_tracks_propios_o_admin" on public.tracks
    for delete using (subido_por = auth.uid() or public.es_admin());

-- ---- Políticas: descargas ----
create policy "ver_descargas_propias_o_admin" on public.descargas
    for select using (usuario_id = auth.uid() or public.es_admin());

create policy "registrar_descarga_propia" on public.descargas
    for insert with check (usuario_id = auth.uid() and public.tiene_acceso_activo());

-- ---- Políticas: pagos ----
-- Los usuarios solo ven sus propios pagos. Insert/update queda reservado
-- a la service_role key (la usa el webhook de Mercado Pago, nunca el cliente).
create policy "ver_pagos_propios_o_admin" on public.pagos
    for select using (usuario_id = auth.uid() or public.es_admin());


-- ============================================================
-- PRIMER ADMIN
-- Después de registrarte por primera vez en la app, corré esto
-- UNA VEZ reemplazando el email por el tuyo, para quedar como admin:
-- ============================================================
-- update public.perfiles set rol = 'admin' where email = 'tu-email@gmail.com';
