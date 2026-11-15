-- ============================================================
-- Catálogo Colaborativo de Remixes — Borrado COMPLETO de usuarios (v17)
-- ============================================================
-- 013_borrar_usuarios.sql dejó anotado que borrar la cuenta de login de
-- verdad (auth.users, con su email y contraseña) "requiere la Admin API
-- de Supabase con la service_role key" — eso en ese momento se asumió
-- imposible sin esa key, así que el botón "Borrar" de Admin solo borra
-- la fila de perfiles y el login queda huérfano, vivo para siempre.
--
-- Esa suposición estaba mal: auth.users es una tabla de Postgres como
-- cualquier otra. Esta función corre con los privilegios de quien la
-- crea (quien ejecute esto en el SQL Editor) vía "security definer" —
-- el mismo mecanismo que ya usa handle_new_user() en schema.sql para
-- crear la fila de perfiles cuando alguien se registra (ese trigger ya
-- toca auth.users con este mismo mecanismo, en la otra dirección). No
-- hace falta la service_role key en ningún momento: la key sigue siendo
-- anon + JWT del usuario, y esta función solo se puede ejecutar siendo
-- admin (chequea es_admin() antes de borrar nada).
--
-- Al borrar la fila de auth.users, el "on delete cascade" que
-- perfiles.id ya tiene hacia auth.users(id) (schema.sql línea 14) borra
-- la fila de perfiles sola, en cadena — y de ahí para abajo, todo el
-- resto de las foreign keys ya definidas en el schema se encargan solas:
--   - descargas, reproducciones, favoritos, mensajes_chat (todas
--     usuario_id ... on delete cascade): se borran con él,
--   - tracks.subido_por, pagos.usuario_id (on delete set null): quedan
--     en null, el tema sigue en el catálogo y el historial de pagos
--     sigue existiendo, solo que sin usuario asociado.
-- Es decir: después de esto, el usuario desaparece de Admin > Usuarios,
-- pierde el login por completo (su email queda libre para registrarse
-- de nuevo si quiere) y no queda ningún resto de su actividad personal
-- — pero lo que subió o pagó sigue intacto en el catálogo/contabilidad.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 016_fix_favoritos_embed.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

create or replace function public.borrar_usuario_completo(p_usuario_id uuid)
returns void
language plpgsql
security definer set search_path = public
as $$
begin
    if not public.es_admin() then
        raise exception 'Solo un administrador puede borrar usuarios.';
    end if;

    if p_usuario_id = auth.uid() then
        raise exception 'No podés borrarte a vos mismo desde acá.';
    end if;

    -- Borra auth.users directamente (no vía Admin API/service_role).
    -- El "on delete cascade" de perfiles.id -> auth.users(id) se encarga
    -- del resto en cadena, como se explica arriba.
    delete from auth.users where id = p_usuario_id;
end;
$$;

comment on function public.borrar_usuario_completo(uuid) is
    'Borrado completo de un usuario (login + perfil + actividad personal), solo-admin. Ver AdminPage.tsx > UsuariosTab > borrar().';

grant execute on function public.borrar_usuario_completo(uuid) to authenticated;
