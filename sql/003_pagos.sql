-- ============================================================
-- Catálogo Colaborativo de Remixes — Suscripción paga (v3)
-- ============================================================
-- Agrega: configuración de suscripción (precio + CBU/CVU para
-- transferencia manual), extensión de la tabla pagos para
-- distinguir Mercado Pago de transferencia manual, y la función
-- interna que usa el webhook de Mercado Pago para activar
-- suscripciones SIN usar la service_role key.
--
-- Es seguro correr esto sobre una base que ya tenga schema.sql y
-- 002_mejoras.sql aplicados (usa "if not exists" / "drop ... if
-- exists" antes de crear).
--
-- Ejecutar en: Supabase > SQL Editor > New query, DESPUÉS de
-- haber corrido sql/schema.sql y sql/002_mejoras.sql.
-- ============================================================

-- ------------------------------------------------------------
-- 0. Por qué esto necesita una función especial
-- ------------------------------------------------------------
-- Todo el resto de esta app (y del backend FastAPI) respeta una
-- regla de seguridad: el backend NUNCA usa la service_role key,
-- solo la anon key + el JWT del usuario que hace la petición. Así
-- las políticas RLS son la única fuente de verdad.
--
-- El webhook de Mercado Pago rompe ese patrón en un punto: cuando
-- Mercado Pago nos avisa "este pago se aprobó", esa notificación
-- NO viene de ningún usuario logueado de la app (no hay JWT de
-- Supabase, MP no tiene sesión acá) y necesita poder activar la
-- suscripción de UN USUARIO DISTINTO al que está haciendo la
-- llamada (no hay "llamada" de usuario en absoluto).
--
-- En vez de usar la service_role key para esto (lo que le daría al
-- backend un bypass total de RLS en todas las tablas), se crea una
-- única función "security definer" que hace SOLO esas dos escrituras
-- puntuales (perfiles.estado_suscripcion + insert en pagos), y que
-- exige un secreto interno como primer parámetro. Ese secreto vive
-- únicamente en el .env del backend (nunca en el navegador) y en la
-- base como un hash (no en texto plano). Si el .env del backend se
-- filtra, el daño posible queda acotado a "puede llamar a esta
-- función puntual", no a "tiene la llave maestra de toda la base".

create extension if not exists pgcrypto;

create table if not exists public.secretos_internos (
    nombre text primary key,
    hash_secreto text not null
);

comment on table public.secretos_internos is
    'Secretos compartidos solo con el backend (nunca con el navegador). Sin policies: nadie puede leerla vía la API REST, ni con anon ni con un usuario logueado — solo la accede la función security definer de abajo, que corre como owner y evita RLS.';

alter table public.secretos_internos enable row level security;
-- A propósito: no se crea ninguna policy. Sin policies + RLS activado
-- = nadie puede hacer select/insert/update/delete sobre esta tabla
-- desde la API REST, pase lo que pase con su rol o su JWT.

-- ------------------------------------------------------------
-- Generar el secreto UNA VEZ (en tu máquina, no en un chat):
--   openssl rand -hex 32
-- Pegá el resultado en el .env del backend como BACKEND_INTERNAL_SECRET
-- y corré esto reemplazando 'PEGA-ACA-EL-SECRETO-GENERADO':
-- ------------------------------------------------------------
-- insert into public.secretos_internos (nombre, hash_secreto) values
--     ('backend_webhook', encode(digest('PEGA-ACA-EL-SECRETO-GENERADO', 'sha256'), 'hex'))
-- on conflict (nombre) do update set hash_secreto = excluded.hash_secreto;


-- ------------------------------------------------------------
-- 1. CONFIGURACIÓN DE SUSCRIPCIÓN
-- Fila única (singleton) que edita el admin: precio mensual y
-- datos de la cuenta para transferencia manual.
-- ------------------------------------------------------------
create table if not exists public.configuracion_suscripcion (
    id boolean primary key default true,
    precio_mensual numeric(10,2) not null default 0,
    moneda text not null default 'ARS',
    cbu_cvu text,
    alias_cbu text,
    titular_cuenta text,
    mp_habilitado boolean not null default false,
    actualizado_por uuid references public.perfiles(id) on delete set null,
    actualizado_en timestamptz not null default now(),
    constraint una_sola_fila check (id)
);

comment on table public.configuracion_suscripcion is
    'Fila única con el precio y los datos de cobro, editable solo por admins';

insert into public.configuracion_suscripcion (id) values (true)
on conflict (id) do nothing;

alter table public.configuracion_suscripcion enable row level security;

drop policy if exists "ver_configuracion_suscripcion" on public.configuracion_suscripcion;
create policy "ver_configuracion_suscripcion" on public.configuracion_suscripcion
    for select using (auth.uid() is not null);
-- A propósito NO usa tiene_acceso_activo(): un usuario vencido o
-- pendiente es justo quien más necesita ver el precio y el CBU para
-- poder pagar. Solo exige estar logueado.

drop policy if exists "admin_edita_configuracion_suscripcion" on public.configuracion_suscripcion;
create policy "admin_edita_configuracion_suscripcion" on public.configuracion_suscripcion
    for update using (public.es_admin()) with check (public.es_admin());


-- ------------------------------------------------------------
-- 2. PAGOS: distinguir Mercado Pago de transferencia manual
-- ------------------------------------------------------------
alter table public.pagos
    add column if not exists tipo text not null default 'mercadopago'
        check (tipo in ('mercadopago', 'manual')),
    add column if not exists confirmado_por uuid references public.perfiles(id) on delete set null,
    add column if not exists comentario text;

comment on column public.pagos.tipo is '''mercadopago'': lo crea la función del webhook. ''manual'': lo crea el propio usuario al avisar una transferencia, y lo aprueba/rechaza un admin';
comment on column public.pagos.confirmado_por is 'Admin que aprobó o rechazó un aviso de transferencia manual (null para pagos de Mercado Pago)';

create index if not exists idx_pagos_tipo_estado on public.pagos (tipo, estado);

-- Un usuario puede avisar SU PROPIA transferencia manual, siempre
-- pendiente — no puede auto-aprobarse ni crearse un pago "mercadopago".
drop policy if exists "avisar_transferencia_manual_propia" on public.pagos;
create policy "avisar_transferencia_manual_propia" on public.pagos
    for insert with check (
        usuario_id = auth.uid()
        and tipo = 'manual'
        and estado = 'pendiente'
    );

-- El admin aprueba/rechaza avisos de transferencia (y podría editar
-- cualquier pago si hiciera falta corregir algo a mano).
drop policy if exists "admin_gestiona_pagos" on public.pagos;
create policy "admin_gestiona_pagos" on public.pagos
    for update using (public.es_admin()) with check (public.es_admin());

-- No hace falta (ni conviene) una policy de insert para
-- tipo='mercadopago': esas filas las crea únicamente la función de
-- abajo, que corre como owner y evita RLS por completo.


-- ------------------------------------------------------------
-- 3. Función interna: procesa lo que llega del webhook de MP
-- ------------------------------------------------------------
-- La llama el backend (con la anon key, SIN JWT de usuario — el
-- webhook no tiene sesión) después de validar la firma x-signature
-- de Mercado Pago. p_secret tiene que matchear el hash guardado en
-- secretos_internos; si no matchea, falla y no escribe nada.
create or replace function public.procesar_notificacion_mp(
    p_secret text,
    p_usuario_id uuid,
    p_mp_subscription_id text default null,
    p_estado_suscripcion text default null,
    p_mp_payment_id text default null,
    p_estado_pago text default null,
    p_monto numeric default null,
    p_moneda text default 'ARS',
    p_payload jsonb default null
)
returns void
language plpgsql
security definer set search_path = public
as $$
declare
    v_hash_guardado text;
begin
    select hash_secreto into v_hash_guardado
    from public.secretos_internos
    where nombre = 'backend_webhook';

    if v_hash_guardado is null or decode(v_hash_guardado, 'hex') != digest(p_secret, 'sha256') then
        raise exception 'secreto inválido para procesar_notificacion_mp';
    end if;

    if p_usuario_id is not null and p_estado_suscripcion is not null then
        update public.perfiles
        set estado_suscripcion = p_estado_suscripcion,
            mp_subscription_id = coalesce(p_mp_subscription_id, mp_subscription_id)
        where id = p_usuario_id;
    end if;

    if p_mp_payment_id is not null then
        insert into public.pagos
            (usuario_id, mp_payment_id, estado, monto, moneda, payload_crudo, tipo)
        values
            (p_usuario_id, p_mp_payment_id, p_estado_pago, p_monto, p_moneda, p_payload, 'mercadopago');
    end if;
end;
$$;

comment on function public.procesar_notificacion_mp is
    'Única puerta de escritura "sin usuario" que usa el webhook de Mercado Pago. Exige el secreto interno (p_secret) para no depender de la service_role key. Ver sección 0 de este archivo.';

-- Se puede llamar sin sesión (anon) porque el webhook no tiene JWT
-- de ningún usuario — el secreto es lo que realmente la protege.
revoke all on function public.procesar_notificacion_mp(
    text, uuid, text, text, text, text, numeric, text, jsonb
) from public;
grant execute on function public.procesar_notificacion_mp(
    text, uuid, text, text, text, text, numeric, text, jsonb
) to anon, authenticated;
