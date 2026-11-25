-- ============================================================
-- Catálogo Colaborativo de Remixes — Precio por tipo de licencia (v22)
-- ============================================================
-- Hasta ahora había UN solo precio mensual para toda la plataforma
-- (configuracion_suscripcion.precio_mensual), aunque desde
-- 019_tipo_licencia.sql ya existen dos niveles de licencia (Simple =
-- solo escucha, DJ = escucha + descarga + carga). Esto agrega un
-- precio propio para cada nivel, y una forma de que el usuario le
-- avise al admin qué licencia quiere (sin poder auto-otorgársela —
-- eso lo sigue decidiendo el admin, como con tipo_licencia).
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 021_notificaciones_push.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ------------------------------------------------------------
-- 1. DOS PRECIOS EN VEZ DE UNO
-- ------------------------------------------------------------
-- Se agregan las dos columnas nuevas y se rellena precio_mensual_dj
-- con el valor que ya tenía precio_mensual (todas las cuentas de
-- antes de 019_tipo_licencia.sql quedaron en 'dj', así que ese precio
-- viejo era, en los hechos, el precio de la licencia DJ). precio_mensual
-- se borra al final — nada más lo lee ni lo escribe en el código
-- después de este cambio.
alter table public.configuracion_suscripcion
    add column if not exists precio_mensual_simple numeric(10,2) not null default 0,
    add column if not exists precio_mensual_dj numeric(10,2);

update public.configuracion_suscripcion
    set precio_mensual_dj = precio_mensual
    where precio_mensual_dj is null;

alter table public.configuracion_suscripcion
    alter column precio_mensual_dj set not null,
    alter column precio_mensual_dj set default 0;

alter table public.configuracion_suscripcion drop column if exists precio_mensual;

comment on column public.configuracion_suscripcion.precio_mensual_simple is
    'Precio mensual de la licencia Simple (solo escucha). Antes de correr esta migración no existía — arranca en 0, el admin tiene que cargarlo en Admin > Suscripción.';
comment on column public.configuracion_suscripcion.precio_mensual_dj is
    'Precio mensual de la licencia DJ (escucha + descarga + carga). Es el valor que antes vivía en precio_mensual (ya migrado, no hace falta recargarlo).';


-- ------------------------------------------------------------
-- 2. LICENCIA QUE EL USUARIO PIDE (no la que tiene)
-- ------------------------------------------------------------
-- tipo_licencia (019_tipo_licencia.sql) es la licencia OTORGADA —
-- protegida por el trigger proteger_campos_perfil, solo la cambia un
-- admin. Esta columna nueva es la licencia que el usuario DICE QUE
-- QUIERE (elegida en la página de Suscripción al elegir con cuál
-- pagar) — a propósito NO está en la lista de columnas protegidas de
-- proteger_campos_perfil, así que cualquier usuario puede escribir la
-- suya propia vía la policy "editar_propio_perfil" que ya existe en
-- schema.sql. El admin la ve en el panel (pestaña Suscripción, junto a
-- cada transferencia pendiente) y decide si la respeta o no al activar
-- la suscripción — igual que hoy decide tipo_licencia a mano.
alter table public.perfiles
    add column if not exists tipo_licencia_solicitada text;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'perfiles_tipo_licencia_solicitada_check'
    ) then
        alter table public.perfiles
            add constraint perfiles_tipo_licencia_solicitada_check
            check (tipo_licencia_solicitada is null or tipo_licencia_solicitada in ('simple', 'dj'));
    end if;
end $$;

comment on column public.perfiles.tipo_licencia_solicitada is
    'Licencia que el usuario eligió en la página de Suscripción (su pedido, no lo otorgado) — a diferencia de tipo_licencia, el propio usuario la puede escribir. El admin la usa de guía al aprobar un pago, pero sigue siendo él quien de verdad cambia tipo_licencia.';
