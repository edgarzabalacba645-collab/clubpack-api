-- ============================================================
-- Fix: idx_tracks_fingerprint rompía la subida de cualquier track
-- cuyo fingerprint de Chromaprint (fpcalc) superara el tamaño máximo
-- que admite un índice btree estándar de Postgres (pasa con temas de
-- duración normal/larga, no son casos raros).
--
-- Se reemplaza por un índice funcional sobre el hash MD5 del
-- fingerprint: el hash siempre mide lo mismo (32 caracteres), entra
-- sin problema en el índice, y sigue sirviendo para detectar
-- duplicados exactos por fingerprint si en el futuro se agrega una
-- consulta que lo use.
--
-- Ejecutar en: Supabase > SQL Editor > New query. Es seguro correrlo
-- aunque ya hayas subido tracks — no borra ni toca datos existentes.
-- ============================================================

drop index if exists public.idx_tracks_fingerprint;
create index if not exists idx_tracks_fingerprint_md5 on public.tracks (md5(audio_fingerprint));
