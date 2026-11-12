-- ============================================================
-- Catálogo Colaborativo de Remixes — Borrado de usuarios (v13)
-- ============================================================
-- Hasta ahora "perfiles" solo tenía políticas de SELECT y UPDATE — ni
-- siquiera un admin podía borrar la fila de un usuario. Esto agrega esa
-- política, para el botón "Borrar" nuevo en Admin > Usuarios.
--
-- OJO — esto borra la fila de perfiles (nombre, rol, estado de
-- suscripción, etc.), que es todo lo que esta app puede tocar con la
-- anon key + RLS. La cuenta de login en sí (auth.users, con su email y
-- contraseña) NO se borra acá: borrarla de verdad requiere la Admin API
-- de Supabase con la service_role key, y este proyecto decidió no usar
-- esa key nunca en el backend (ver política de seguridad del proyecto).
--
-- Después de borrar a alguien acá:
--   - desaparece de Admin > Usuarios y pierde todo acceso al catálogo
--     (sin fila en perfiles, tiene_acceso_activo() y es_admin() dan
--     false para esa cuenta),
--   - sus temas subidos NO se borran (tracks.subido_por queda en null,
--     el tema sigue en el catálogo sin autor asociado),
--   - sus mensajes del chat SÍ se borran con él
--     (mensajes_chat.usuario_id es "not null ... on delete cascade"),
--   - su email NO queda libre para registrarse de nuevo, porque la
--     cuenta de auth.users sigue existiendo. Si además de borrarlo acá
--     querés eso, hace falta borrar la cuenta a mano en Supabase
--     Dashboard → Authentication → Users.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 012_freemium_v2_vidriera.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

drop policy if exists "admin_borra_perfiles" on public.perfiles;
create policy "admin_borra_perfiles" on public.perfiles
    for delete using (public.es_admin());
