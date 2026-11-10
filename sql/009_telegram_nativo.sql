-- ============================================================
-- Aviso de Telegram nativo en Postgres (sin pasar por Render)
-- ============================================================
-- Por qué: el aviso de "nuevo registro" dependía de que Render (plan
-- gratis) estuviera despierto en el momento exacto en que llega el
-- Database Webhook de Supabase — si el backend estaba dormido (se
-- duerme a los 15 min sin tráfico), el webhook llegaba mientras
-- todavía estaba despertando (~60 seg) y se perdía: pasó en una
-- prueba temprana y volvió a pasar con un registro real el
-- 2026-11-10. Subir el plan de Render o forzarlo a no dormirse nunca
-- (ping constante) no sirve: el plan gratis tiene un tope de 750 horas
-- de instancia por MES, y mantenerlo siempre despierto se come
-- prácticamente todo ese cupo.
--
-- La solución: que Postgres le avise a Telegram DIRECTO, sin pasar
-- por el backend — usando pg_net (extensión de Supabase para hacer
-- HTTP desde SQL). Así el aviso no depende de que Render esté
-- despierto, nunca se duerme ni gasta horas: dispara en el mismo
-- trigger que ya crea la fila en public.perfiles.
--
-- El endpoint /webhooks/nuevo-registro de Render (app/routers/
-- webhooks.py) queda sin usar después de este cambio — no hace falta
-- borrarlo, pero conviene sacar el Database Webhook que lo llama en
-- Supabase (Database > Webhooks) para no mandar el aviso DOS veces
-- (una acá, directo, y otra si Render llega a estar despierto).
--
-- Seguro de correr sobre una base que ya tenga schema.sql ..
-- 008_peaks_waveform.sql aplicados.
--
-- IMPORTANTE — hay que completar esto ANTES de correr el resto del
-- archivo: reemplazar los dos placeholders de más abajo
-- (PEGAR_TOKEN_ACA / PEGAR_CHAT_ID_ACA) por los valores reales, los
-- mismos que ya están cargados en Render como TELEGRAM_BOT_TOKEN y
-- TELEGRAM_CHAT_ID. Si se corre tal cual, con los placeholders, el
-- aviso va a fallar en silencio (pg_net no tira error visible acá,
-- solo queda registrado en net._http_response — ver la consulta de
-- verificación al final de este archivo).
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

-- 1. Habilitar pg_net (si ya está habilitada, esto no hace nada).
create extension if not exists pg_net with schema extensions;

-- 2. Guardar el token del bot y el chat id en Vault, en vez de
--    dejarlos sueltos en el código SQL. Esta sección se corre UNA
--    sola vez — si se vuelve a correr tal cual, va a fallar porque
--    el nombre del secreto ya existe (son únicos). Si en el futuro
--    hay que cambiar el token o el chat id, usar vault.update_secret
--    con el id que devuelve vault.create_secret, no volver a insertar.
select vault.create_secret('PEGAR_TOKEN_ACA', 'telegram_bot_token', 'Bot de Telegram de ClubPack (@BotFather)');
select vault.create_secret('PEGAR_CHAT_ID_ACA', 'telegram_chat_id', 'Chat id del admin de ClubPack (@userinfobot)');

-- 3. Función que arma y manda el mensaje. security definer porque
--    necesita leer vault.decrypted_secrets, a la que un usuario común
--    no tiene acceso.
create or replace function public.notificar_registro_telegram()
returns trigger
language plpgsql
security definer set search_path = public, extensions
as $$
declare
    v_token text;
    v_chat_id text;
    v_quien text;
    v_texto text;
begin
    select decrypted_secret into v_token
        from vault.decrypted_secrets where name = 'telegram_bot_token';
    select decrypted_secret into v_chat_id
        from vault.decrypted_secrets where name = 'telegram_chat_id';

    -- Si todavía no se cargaron los secretos (placeholders sin
    -- reemplazar, o recién habilitado esto), no rompe el registro del
    -- usuario — simplemente no manda nada, igual que hacía
    -- app/telegram.py cuando faltaba la config.
    if v_token is null or v_chat_id is null then
        return new;
    end if;

    v_quien := case
        when new.nombre_dj is not null and new.nombre_dj <> new.email
            then new.nombre_dj || ' (' || new.email || ')'
        else coalesce(new.email, '(sin email)')
    end;
    v_texto := '🆕 <b>Nuevo registro en ClubPack</b>' || chr(10) || v_quien
        || chr(10) || chr(10) || 'Entrá al panel de Admin para activarle la suscripción.';

    perform net.http_post(
        url := 'https://api.telegram.org/bot' || v_token || '/sendMessage',
        body := jsonb_build_object('chat_id', v_chat_id, 'text', v_texto, 'parse_mode', 'HTML'),
        headers := '{"Content-Type": "application/json"}'::jsonb,
        timeout_milliseconds := 5000
    );

    return new;
end;
$$;

-- 4. Trigger: se dispara apenas se crea la fila en perfiles (el mismo
--    momento en que antes disparaba el Database Webhook hacia Render).
drop trigger if exists on_perfil_creado_avisar_telegram on public.perfiles;
create trigger on_perfil_creado_avisar_telegram
    after insert on public.perfiles
    for each row execute function public.notificar_registro_telegram();

-- ------------------------------------------------------------
-- Verificación: después de probar un registro nuevo, correr esto
-- para ver si pg_net pudo mandar el mensaje (200 = OK; si no aparece
-- ninguna fila, el trigger no llegó a dispararse; si aparece con
-- status_code distinto de 200 o con error_msg, ahí está el motivo).
-- ------------------------------------------------------------
-- select created, status_code, content, error_msg
-- from net._http_response
-- order by created desc
-- limit 5;
