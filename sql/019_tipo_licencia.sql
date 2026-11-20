-- ============================================================
-- Catálogo Colaborativo de Remixes — Licencia Simple / DJ (v19)
-- ============================================================
-- Hasta ahora, cualquier suscriptor con acceso activo podía escuchar,
-- descargar Y subir temas por igual. Esto agrega un segundo nivel de
-- suscripción más barato, pensado para gente que solo quiere escuchar
-- (no DJs): "Simple" (solo escucha) vs. "DJ" (escucha + descarga +
-- carga, como hoy).
--
-- OJO con el orden de estos 3 pasos — es a propósito así, para no
-- voltear a nadie que ya exista:
--   1. Se agrega la columna SIN default todavía.
--   2. Se rellenan TODOS los que ya existen con 'dj' — así nadie que
--      ya tenía descarga/carga las pierde con esta migración.
--   3. Recién ahí se le pone default 'simple' a la columna — eso solo
--      afecta a una fila NUEVA (alguien que se registre de ahora en
--      más), no a las que ya estaban.
-- Si se hiciera al revés (agregar la columna ya con default 'simple'),
-- Postgres rellena con ese default TAMBIÉN a los que ya existen, y de
-- un saque te quedarían todos los DJs actuales sin poder descargar.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 018_ranking_mas_escuchados.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

alter table public.perfiles add column if not exists tipo_licencia text;

update public.perfiles set tipo_licencia = 'dj' where tipo_licencia is null;

alter table public.perfiles alter column tipo_licencia set default 'simple';
alter table public.perfiles alter column tipo_licencia set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'perfiles_tipo_licencia_check'
    ) then
        alter table public.perfiles
            add constraint perfiles_tipo_licencia_check check (tipo_licencia in ('simple', 'dj'));
    end if;
end $$;

comment on column public.perfiles.tipo_licencia is
    'simple = solo escucha. dj = escucha + descarga + carga (como era todo antes de esta columna). Default para altas nuevas: simple — el admin pasa a dj a mano a quien corresponda.';


-- ¿El usuario actual puede DESCARGAR? (no solo escuchar). Admin
-- siempre puede; un suscriptor con acceso activo puede solo si además
-- es tipo_licencia = 'dj'. Mismo patrón que puede_subir_temas() de
-- 010_control_subidas.sql — pensada para usarse tanto desde el
-- backend (ver app/deps.py) como desde la policy de insert de
-- `descargas` de abajo.
create or replace function public.puede_descargar()
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select public.es_admin() or (
        public.tiene_acceso_activo()
        and (select tipo_licencia from public.perfiles where id = auth.uid()) = 'dj'
    );
$$;

-- puede_subir_temas() pasa a exigir también tipo_licencia = 'dj' — un
-- usuario Simple no puede subir temas aunque el admin tenga las
-- subidas de colaboradores habilitadas en general.
create or replace function public.puede_subir_temas()
returns boolean
language sql
security definer set search_path = public
stable
as $$
    select public.es_admin() or (
        public.tiene_acceso_activo()
        and (select tipo_licencia from public.perfiles where id = auth.uid()) = 'dj'
        and coalesce(
            (select permite_subida_colaboradores from public.configuracion_catalogo where id = true),
            true
        )
    );
$$;

-- La policy de insert de `descargas` pasa a exigir puede_descargar()
-- en vez de tiene_acceso_activo() directo — mismo motivo que la de
-- tracks en 010_control_subidas.sql: aunque algo del lado del backend
-- se salte el chequeo, RLS sigue siendo la única fuente de verdad real
-- de quién puede registrar una descarga.
drop policy if exists "registrar_descarga_propia" on public.descargas;
create policy "registrar_descarga_propia" on public.descargas
    for insert with check (usuario_id = auth.uid() and public.puede_descargar());


-- IMPORTANTE: 005_features.sql ya instaló un trigger
-- (proteger_campos_perfil) que, en un UPDATE a perfiles hecho por
-- alguien que no es admin, pisa rol/estado_suscripcion/
-- mp_subscription_id/email con su valor viejo — para que nadie se
-- auto-asigne admin o se active la suscripción con un PATCH directo a
-- la API de Supabase. tipo_licencia necesita exactamente la misma
-- protección (si no, cualquier Simple se auto-asciende a DJ a mano),
-- así que se agrega acá — create or replace de la misma función,
-- ahora protegiendo también esta columna.
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
        new.tipo_licencia := old.tipo_licencia;
    end if;
    return new;
end;
$$;
