-- ============================================================
-- Catálogo Colaborativo de Remixes — Mejoras (v2)
-- ============================================================
-- Agrega: géneros administrables, fingerprint de audio + estado
-- en tracks, playlists, y tabla de duplicados detectados.
--
-- Es seguro correr esto tanto sobre una base recién creada con
-- schema.sql como sobre una que ya esté en uso (todo usa
-- "if not exists" / "drop ... if exists" antes de crear).
--
-- Ejecutar en: Supabase > SQL Editor > New query, DESPUÉS de
-- haber corrido sql/schema.sql.
-- ============================================================

-- ------------------------------------------------------------
-- 1. GÉNEROS (administrable desde el panel de Admin)
-- ------------------------------------------------------------
create table if not exists public.generos (
    id uuid primary key default gen_random_uuid(),
    nombre text not null unique,
    activo boolean not null default true,
    orden integer not null default 0
);

comment on table public.generos is 'Lista de géneros disponibles, gestionada por los admins';

-- Géneros típicos para arrancar (no pisa nada si ya existen)
insert into public.generos (nombre, orden) values
    ('Cuarteto', 1),
    ('Cumbia', 2),
    ('Reggaeton', 3),
    ('Electrónica', 4),
    ('Pop', 5),
    ('Rock', 6),
    ('Trap', 7),
    ('Folklore', 8)
on conflict (nombre) do nothing;

alter table public.tracks
    add column if not exists genero_id uuid references public.generos(id) on delete set null;

create index if not exists idx_tracks_genero_id on public.tracks (genero_id);

comment on column public.tracks.genero_id is 'FK a generos. La columna genero (texto) queda como respaldo para tracks viejos sin migrar';


-- ------------------------------------------------------------
-- 2. TRACKS: fingerprint de audio + estado
-- ------------------------------------------------------------
alter table public.tracks
    add column if not exists audio_fingerprint text,
    add column if not exists estado text not null default 'activo'
        check (estado in ('activo', 'duplicado_sospechoso', 'eliminado'));

-- Índice funcional sobre el hash MD5 del fingerprint: un fingerprint de
-- Chromaprint puede ser más largo de lo que admite un índice btree
-- estándar (pasa con temas largos), así que se indexa su hash en vez
-- del valor crudo — sigue sirviendo para detectar duplicados exactos.
create index if not exists idx_tracks_fingerprint_md5 on public.tracks (md5(audio_fingerprint));
create index if not exists idx_tracks_estado on public.tracks (estado);

comment on column public.tracks.audio_fingerprint is 'Fingerprint de Chromaprint (fpcalc) del audio, para detectar el mismo audio subido con otro nombre';


-- ------------------------------------------------------------
-- 3. DUPLICADOS DETECTADOS
-- Resultado de un escaneo (manual, desde Admin) que compara
-- todos los tracks entre sí por nombre y por fingerprint.
-- ------------------------------------------------------------
create table if not exists public.duplicados_detectados (
    id uuid primary key default gen_random_uuid(),
    track_id_a uuid not null references public.tracks(id) on delete cascade,
    track_id_b uuid not null references public.tracks(id) on delete cascade,
    tipo text not null check (tipo in ('fingerprint_exacto', 'texto_similar')),
    similitud numeric(5,2),             -- 0-100, solo aplica a texto_similar
    estado text not null default 'pendiente'
        check (estado in ('pendiente', 'descartado', 'resuelto')),
    fecha_deteccion timestamptz not null default now(),
    unique (track_id_a, track_id_b, tipo)
);

create index if not exists idx_duplicados_estado on public.duplicados_detectados (estado);

comment on table public.duplicados_detectados is 'Pares de tracks sospechosos de ser el mismo audio, para revisión manual del admin';


-- ------------------------------------------------------------
-- 4. PLAYLISTS
-- ------------------------------------------------------------
create table if not exists public.playlists (
    id uuid primary key default gen_random_uuid(),
    nombre text not null,
    descripcion text,
    creado_por uuid references public.perfiles(id) on delete set null,
    fecha_creacion timestamptz not null default now()
);

create table if not exists public.playlist_tracks (
    playlist_id uuid not null references public.playlists(id) on delete cascade,
    track_id uuid not null references public.tracks(id) on delete cascade,
    orden integer not null default 0,
    primary key (playlist_id, track_id)
);


-- ============================================================
-- ROW LEVEL SECURITY — tablas nuevas
-- ============================================================
alter table public.generos enable row level security;
alter table public.duplicados_detectados enable row level security;
alter table public.playlists enable row level security;
alter table public.playlist_tracks enable row level security;

-- ---- Géneros: todos con acceso activo pueden leer; solo admin escribe ----
drop policy if exists "ver_generos_con_acceso" on public.generos;
create policy "ver_generos_con_acceso" on public.generos
    for select using (public.tiene_acceso_activo());

drop policy if exists "admin_gestiona_generos" on public.generos;
create policy "admin_gestiona_generos" on public.generos
    for all using (public.es_admin()) with check (public.es_admin());

-- ---- Duplicados: solo admin ----
drop policy if exists "admin_ve_duplicados" on public.duplicados_detectados;
create policy "admin_ve_duplicados" on public.duplicados_detectados
    for all using (public.es_admin()) with check (public.es_admin());

-- ---- Playlists: cualquiera con acceso ve todas; dueño o admin edita ----
drop policy if exists "ver_playlists_con_acceso" on public.playlists;
create policy "ver_playlists_con_acceso" on public.playlists
    for select using (public.tiene_acceso_activo());

drop policy if exists "crear_playlist_con_acceso" on public.playlists;
create policy "crear_playlist_con_acceso" on public.playlists
    for insert with check (public.tiene_acceso_activo() and creado_por = auth.uid());

drop policy if exists "editar_playlist_propia_o_admin" on public.playlists;
create policy "editar_playlist_propia_o_admin" on public.playlists
    for update using (creado_por = auth.uid() or public.es_admin());

drop policy if exists "borrar_playlist_propia_o_admin" on public.playlists;
create policy "borrar_playlist_propia_o_admin" on public.playlists
    for delete using (creado_por = auth.uid() or public.es_admin());

-- ---- Playlist_tracks: mismo criterio, a través de la playlist dueña ----
drop policy if exists "ver_playlist_tracks_con_acceso" on public.playlist_tracks;
create policy "ver_playlist_tracks_con_acceso" on public.playlist_tracks
    for select using (public.tiene_acceso_activo());

drop policy if exists "gestionar_playlist_tracks_propia_o_admin" on public.playlist_tracks;
create policy "gestionar_playlist_tracks_propia_o_admin" on public.playlist_tracks
    for all using (
        public.es_admin()
        or exists (
            select 1 from public.playlists p
            where p.id = playlist_id and p.creado_por = auth.uid()
        )
    );


-- ============================================================
-- TRACKS: excluir eliminados de la vista normal del catálogo
-- (se pisa la política de v1 para sumar el filtro por estado)
-- ============================================================
drop policy if exists "ver_tracks_con_acceso" on public.tracks;
create policy "ver_tracks_con_acceso" on public.tracks
    for select using (public.tiene_acceso_activo() and estado != 'eliminado');

-- Los admins necesitan poder ver también los eliminados/sospechosos
-- (para el panel de mantenimiento de duplicados)
drop policy if exists "admin_ve_todos_los_tracks" on public.tracks;
create policy "admin_ve_todos_los_tracks" on public.tracks
    for select using (public.es_admin());
