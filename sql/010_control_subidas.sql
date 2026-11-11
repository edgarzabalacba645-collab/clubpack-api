-- ------------------------------------------------------------
-- Control de subidas: el admin puede cerrar la subida de temas a los
-- suscriptores (dejarla solo para sí mismo) cuando el catálogo recién
-- arranca y conviene revisar/prolijar lo que entra a mano, en vez de
-- que cualquiera suba con nombres mal puestos, género equivocado, etc.
--
-- Mismo patrón que configuracion_suscripcion (sql/003_pagos.sql):
-- fila única (singleton), visible para cualquier logueado, editable
-- solo por admin.
-- ------------------------------------------------------------

create table if not exists public.configuracion_catalogo (
    id boolean primary key default true,
    permite_subida_colaboradores boolean not null default true,
    actualizado_por uuid references public.perfiles(id) on delete set null,
    actualizado_en timestamptz not null default now(),
    constraint una_sola_fila check (id)
);

comment on table public.configuracion_catalogo is
    'Fila única con configuración general del catálogo — por ahora, si los suscriptores pueden subir temas ellos mismos o si por ahora queda solo para el admin. Editable solo por admins.';

insert into public.configuracion_catalogo (id) values (true)
on conflict (id) do nothing;

alter table public.configuracion_catalogo enable row level security;

drop policy if exists "ver_configuracion_catalogo" on public.configuracion_catalogo;
create policy "ver_configuracion_catalogo" on public.configuracion_catalogo
    for select using (auth.uid() is not null);
-- A propósito no exige tiene_acceso_activo(): el frontend necesita leer
-- este flag para decidir qué mostrar (botón "Subir Temas", texto del
-- hero) incluso para un usuario sin suscripción activa.

drop policy if exists "admin_edita_configuracion_catalogo" on public.configuracion_catalogo;
create policy "admin_edita_configuracion_catalogo" on public.configuracion_catalogo
    for update using (public.es_admin()) with check (public.es_admin());


-- Función helper: ¿el usuario actual puede subir temas? Admin siempre
-- puede; un suscriptor con acceso activo puede solo si el admin lo
-- tiene habilitado. Centraliza el criterio para usarlo tanto en la
-- policy de insert de tracks (abajo) como desde el backend
-- (app/deps.py::requiere_puede_subir), vía rpc — mismo patrón que
-- tiene_acceso_activo()/es_admin().
create or replace function public.puede_subir_temas()
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select public.es_admin() or (
        public.tiene_acceso_activo()
        and coalesce(
            (select permite_subida_colaboradores from public.configuracion_catalogo where id = true),
            true
        )
    );
$$;

-- La policy de insert de tracks pasa a exigir puede_subir_temas() en
-- vez de tiene_acceso_activo() directo — así, aunque alguien se salte
-- el chequeo del backend (/tracks/confirm), RLS sigue siendo la única
-- fuente de verdad real de quién puede subir (ver README del backend).
drop policy if exists "subir_tracks_con_acceso" on public.tracks;
create policy "subir_tracks_con_acceso" on public.tracks
    for insert with check (public.puede_subir_temas() and subido_por = auth.uid());
