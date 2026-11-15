-- ============================================================
-- Catálogo Colaborativo de Remixes — Ranking de más escuchados (v18)
-- ============================================================
-- Mismo patrón que obtener_ranking_top_50() (007_top50_para_todos.sql),
-- pero contando reproducciones en vez de descargas: cuenta agregada por
-- track (sin usuario_id ni fecha), security definer para poder sumar
-- las reproducciones de todo el grupo, con el mismo gate
-- tiene_acceso_activo() que ya usa el de descargas — sin suscripción
-- activa (ni ser admin), el ranking sale vacío.
--
-- Las reproducciones ya se anotan desde antes (ver
-- 014_actividad_por_usuario.sql: registrar_reproduccion(), que llama
-- el player una vez por tema que arranca a sonar de verdad), así que
-- esto no necesita ningún cambio del lado del frontend que registra
-- — solo agrega una forma nueva de leer lo que ya se venía guardando.
--
-- Es seguro correr esto sobre una base que ya tenga hasta
-- 017_borrar_usuario_completo.sql aplicado.
--
-- Ejecutar en: Supabase > SQL Editor > New query.
-- ============================================================

create or replace function public.obtener_ranking_mas_escuchados()
returns table (
    track_id uuid,
    titulo text,
    remixer text,
    reproducciones bigint
)
language plpgsql
security definer set search_path = public
stable
as $$
begin
    if not public.tiene_acceso_activo() then
        return;
    end if;

    return query
        select t.id, t.titulo, t.remixer, count(r.id)::bigint as reproducciones
        from public.reproducciones r
        join public.tracks t on t.id = r.track_id
        group by t.id, t.titulo, t.remixer
        order by count(r.id) desc, t.titulo asc
        limit 50;
end;
$$;

comment on function public.obtener_ranking_mas_escuchados is
    'Top 50 tracks más escuchados (reproducciones reales, no descargas), agregado por track (sin exponer quién ni cuándo) — mismo criterio y gate que obtener_ranking_top_50.';

grant execute on function public.obtener_ranking_mas_escuchados() to authenticated;
