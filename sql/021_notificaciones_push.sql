-- ============================================================
-- Notificación push real cuando te arroban (@) en el chat
-- ============================================================
-- Agrega: tabla de suscripciones push (Web Push / VAPID) por usuario y
-- dispositivo, la columna de menciones en mensajes_chat, y un trigger
-- que avisa al backend apenas se inserta un mensaje con alguna mención
-- — el backend (app/push.py) es quien de verdad manda la notificación
-- al celular (la encriptación de Web Push no es algo razonable de
-- hacer en SQL puro).
--
-- Por qué pasa por el backend y no 100% por Postgres (a diferencia del
-- aviso de Telegram de 009_telegram_nativo.sql): Telegram solo necesita
-- un POST con un token de bot — pg_net alcanza. Web Push en cambio
-- exige firmar un JWT VAPID (ES256) y encriptar el payload en el
-- momento con las claves propias de CADA suscripción (ECDH + AES-GCM)
-- — eso sí necesita una librería de verdad (pywebpush), no algo
-- razonable de reimplementar en PL/pgSQL. Mitigación del mismo riesgo
-- que motivó 009 (Render dormido): el trigger no espera la respuesta
-- (pg_net es asíncrono) y se le da un timeout generoso (20s) para que
-- alcance a terminar de despertar si hacía falta — en la práctica el
-- chat se usa con la app abierta, que ya mantiene despierto a Render
-- con su ping cada 10 min (ver usarDespertarBackend en App.tsx), así
-- que esta ventana rara vez se da.
--
-- Es seguro correr esto sobre una base que ya tenga schema.sql ..
-- 020_fingerprint_similitud.sql aplicados.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- ------------------------------------------------------------
-- 1. SUSCRIPCIONES PUSH (una fila por navegador/dispositivo que activó
--    notificaciones — un mismo usuario puede tener varias, ej. celular
--    + PC).
-- ------------------------------------------------------------
create table if not exists public.push_subscripciones (
    id uuid primary key default gen_random_uuid(),
    usuario_id uuid not null references public.perfiles(id) on delete cascade,
    endpoint text not null unique,
    p256dh text not null,
    auth text not null,
    creado_en timestamptz not null default now()
);

comment on table public.push_subscripciones is
    'Suscripciones Web Push (una por navegador/dispositivo que activó notificaciones) — las usa app/push.py del backend para avisar cuando arroban a alguien en el chat.';

create index if not exists idx_push_subscripciones_usuario on public.push_subscripciones (usuario_id);

alter table public.push_subscripciones enable row level security;

-- El propio usuario gestiona sus suscripciones (activar/desactivar
-- notificaciones en Mi Cuenta, ver lib/push.ts) — directo desde el
-- frontend contra Supabase, sin pasar por el backend (no necesita R2
-- ni ninguna otra credencial, como el resto de las operaciones simples
-- de esta app).
drop policy if exists "ver_propia_suscripcion_push" on public.push_subscripciones;
create policy "ver_propia_suscripcion_push" on public.push_subscripciones
    for select using (usuario_id = auth.uid());

drop policy if exists "crear_propia_suscripcion_push" on public.push_subscripciones;
create policy "crear_propia_suscripcion_push" on public.push_subscripciones
    for insert with check (usuario_id = auth.uid());

drop policy if exists "actualizar_propia_suscripcion_push" on public.push_subscripciones;
create policy "actualizar_propia_suscripcion_push" on public.push_subscripciones
    for update using (usuario_id = auth.uid()) with check (usuario_id = auth.uid());

drop policy if exists "borrar_propia_suscripcion_push" on public.push_subscripciones;
create policy "borrar_propia_suscripcion_push" on public.push_subscripciones
    for delete using (usuario_id = auth.uid());


-- ------------------------------------------------------------
-- 2. MENCIONES EN EL CHAT
-- ------------------------------------------------------------
-- Array de ids de perfiles arrobados en el mensaje (lo arma el
-- frontend al enviar, ver ChatPage.tsx — resuelve cada "@Nombre" del
-- autocompletado a un id real, nunca texto libre). No hace falta
-- tocar las policies de mensajes_chat: el insert ya exige
-- usuario_id = auth.uid(), este es solo un dato más de la misma fila.
alter table public.mensajes_chat
    add column if not exists menciones uuid[] not null default '{}'::uuid[];

comment on column public.mensajes_chat.menciones is
    'Ids de perfiles arrobados (@) en este mensaje — dispara la notificación push de public.notificar_mencion_chat().';


-- ------------------------------------------------------------
-- 3. LISTAR DJs PARA EL AUTOCOMPLETADO DE "@"
-- ------------------------------------------------------------
-- Por qué hace falta esto: la policy de perfiles (ver schema.sql,
-- "ver_propio_perfil") solo deja ver la fila PROPIA (o todas, si sos
-- admin) — un suscriptor común no puede leer el nombre_dj de otro
-- perfil para saber a quién está arrobando. Esta función security
-- definer expone SOLO id + nombre (nada sensible) de todo el grupo, y
-- solo a quien ya tiene acceso activo al chat.
--
-- Nota aparte (no se toca en esta migración, pero vale dejarlo
-- anotado): esta misma restricción de RLS hace que, fuera de acá,
-- cualquier "perfiles(nombre_dj, email)" embebido en una consulta de
-- un suscriptor común (el autor de un mensaje de chat, el "subido por"
-- del catálogo, etc.) devuelva null para mensajes/tracks que no son
-- propios — hoy probablemente solo el admin ve esos nombres bien. Es
-- un tema aparte del pedido de las menciones, queda para otra sesión.
create or replace function public.listar_djs_para_mencion()
returns table(id uuid, nombre text)
language sql
security definer set search_path = public
stable
as $$
    select p.id, coalesce(nullif(p.nombre_dj, ''), p.email) as nombre
    from public.perfiles p
    where public.tiene_acceso_activo()
    order by nombre;
$$;


-- ------------------------------------------------------------
-- 4. SECRETO COMPARTIDO (Postgres -> backend), vía Vault
-- ------------------------------------------------------------
-- Mismo patrón que telegram_bot_token en 009_telegram_nativo.sql: el
-- trigger de abajo necesita el valor EN TEXTO PLANO para mandarlo como
-- header al backend (a diferencia de BACKEND_INTERNAL_SECRET, que acá
-- solo vive como hash — ver 003_pagos.sql — porque ahí es el backend
-- quien llama a Postgres, no al revés).
--
-- Si hace falta volver a correr esto (ya existe el secreto), usar
-- vault.update_secret con el id que devuelve vault.create_secret —
-- no insertar de nuevo, el nombre es único.
select vault.create_secret(
    '7d23ff5661bd3e2bec8576b287ffb15002862d10ba4707f27c1c98e865fa17c0',
    'mencion_webhook_secret',
    'Secreto compartido con /webhooks/mencion-chat del backend (mismo valor que MENCION_WEBHOOK_SECRET en Render)'
);


-- ------------------------------------------------------------
-- 5. TRIGGER: avisa al backend apenas se inserta un mensaje con menciones
-- ------------------------------------------------------------
create or replace function public.notificar_mencion_chat()
returns trigger
language plpgsql
security definer set search_path = public, extensions
as $$
declare
    v_secret text;
    v_autor_nombre text;
begin
    if new.menciones is null or array_length(new.menciones, 1) is null then
        return new;
    end if;

    select decrypted_secret into v_secret
        from vault.decrypted_secrets where name = 'mencion_webhook_secret';

    -- Si todavía no se cargó el secreto, no rompe el envío del mensaje
    -- — simplemente no se manda ninguna notificación (igual que el
    -- resto de los avisos "mejor esfuerzo" de esta app).
    if v_secret is null then
        return new;
    end if;

    select coalesce(nullif(nombre_dj, ''), email) into v_autor_nombre
    from public.perfiles where id = new.usuario_id;

    perform net.http_post(
        url := 'https://clubpack-api.onrender.com/webhooks/mencion-chat',
        body := jsonb_build_object(
            'mensaje_id', new.id,
            'autor_nombre', coalesce(v_autor_nombre, 'Alguien'),
            'texto', new.mensaje,
            'menciones', to_jsonb(new.menciones)
        ),
        headers := jsonb_build_object('Content-Type', 'application/json', 'X-Webhook-Secret', v_secret),
        -- Generoso a propósito (ver nota al principio del archivo): si
        -- Render estaba dormido, le da margen para despertar y
        -- terminar de procesar sin que esto bloquee nada (pg_net es
        -- asíncrono, el insert del mensaje ya terminó hace rato).
        timeout_milliseconds := 20000
    );

    return new;
end;
$$;

drop trigger if exists on_mensaje_chat_avisar_mencion on public.mensajes_chat;
create trigger on_mensaje_chat_avisar_mencion
    after insert on public.mensajes_chat
    for each row execute function public.notificar_mencion_chat();


-- ------------------------------------------------------------
-- 6. FUNCIONES QUE USA EL BACKEND (con el secreto YA conocido,
--    BACKEND_INTERNAL_SECRET — mismo patrón que procesar_notificacion_mp
--    en 003_pagos.sql)
-- ------------------------------------------------------------

-- El backend las usa para buscar a quién mandarle el push (lee
-- push_subscripciones de usuarios QUE NO SON el que llama — por eso no
-- alcanza con RLS normal, hace falta esta función puntual).
create or replace function public.obtener_suscripciones_push_de(
    p_secret text,
    p_usuario_ids uuid[]
)
returns table(usuario_id uuid, endpoint text, p256dh text, auth text)
language plpgsql
-- "extensions" hace falta para encontrar digest() (pgcrypto vive ahí
-- en Supabase, no en public) — sin esto la función explota con
-- "function digest(text, unknown) does not exist" apenas se la llama.
security definer set search_path = public, extensions
as $$
declare
    v_hash_guardado text;
begin
    select hash_secreto into v_hash_guardado
    from public.secretos_internos
    where nombre = 'backend_webhook';

    if v_hash_guardado is null or decode(v_hash_guardado, 'hex') != digest(p_secret, 'sha256') then
        raise exception 'secreto inválido para obtener_suscripciones_push_de';
    end if;

    return query
        select s.usuario_id, s.endpoint, s.p256dh, s.auth
        from public.push_subscripciones s
        where s.usuario_id = any(p_usuario_ids);
end;
$$;

comment on function public.obtener_suscripciones_push_de(text, uuid[]) is
    'Único lugar por el que el backend lee suscripciones push de OTROS usuarios — exige el secreto interno (p_secret), igual que procesar_notificacion_mp. Ver sección 0 de 003_pagos.sql.';

revoke all on function public.obtener_suscripciones_push_de(text, uuid[]) from public;
grant execute on function public.obtener_suscripciones_push_de(text, uuid[]) to anon, authenticated;


-- El backend la llama cuando Web Push responde 404/410 (la suscripción
-- ya no existe del lado del navegador — se desinstaló, revocó el
-- permiso, etc.) para no seguir intentando mandarle algo en vano.
create or replace function public.borrar_suscripcion_push(
    p_secret text,
    p_endpoint text
)
returns void
language plpgsql
security definer set search_path = public, extensions
as $$
declare
    v_hash_guardado text;
begin
    select hash_secreto into v_hash_guardado
    from public.secretos_internos
    where nombre = 'backend_webhook';

    if v_hash_guardado is null or decode(v_hash_guardado, 'hex') != digest(p_secret, 'sha256') then
        raise exception 'secreto inválido para borrar_suscripcion_push';
    end if;

    delete from public.push_subscripciones where endpoint = p_endpoint;
end;
$$;

comment on function public.borrar_suscripcion_push(text, text) is
    'Limpieza de suscripciones muertas — la llama app/push.py cuando Web Push devuelve 404/410. Mismo secreto interno que obtener_suscripciones_push_de.';

revoke all on function public.borrar_suscripcion_push(text, text) from public;
grant execute on function public.borrar_suscripcion_push(text, text) to anon, authenticated;


-- ------------------------------------------------------------
-- Verificación: después de mandar un mensaje arrobando a alguien,
-- correr esto para ver si pg_net pudo avisarle al backend (200 = OK;
-- si no aparece ninguna fila, el trigger no llegó a dispararse —
-- revisar que el mensaje realmente tenga algo en menciones).
-- ------------------------------------------------------------
-- select created, status_code, content, error_msg
-- from net._http_response
-- order by created desc
-- limit 5;
