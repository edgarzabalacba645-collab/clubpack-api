-- Antes, el chequeo de audio (tipo 'fingerprint_exacto') exigía que el
-- fingerprint de Chromaprint fuera IDÉNTICO byte a byte (vía
-- audio_fingerprint_md5) — eso solo agarraba el mismo archivo re-subido
-- tal cual. Un fingerprint de audio está pensado para compararse por
-- SIMILITUD (tolera otro bitrate, unos segundos recortados al
-- principio/final, etc. — justo lo que pasa con el mismo tema re-subido
-- con un corte/edit levemente distinto), no por igualdad exacta — ver
-- app/duplicados.py (_similitud_audio).
--
-- Los pares nuevos se guardan de acá en más con tipo 'fingerprint_similar'
-- y su similitud real (0-100, misma escala que texto_similar). Se deja
-- 'fingerprint_exacto' en la constraint solo por compatibilidad con pares
-- que ya estaban guardados de antes — no hace falta migrarlos, siguen
-- siendo duplicados reales.
alter table public.duplicados_detectados drop constraint duplicados_detectados_tipo_check;
alter table public.duplicados_detectados add constraint duplicados_detectados_tipo_check
    check (tipo in ('fingerprint_exacto', 'fingerprint_similar', 'texto_similar'));

comment on column public.duplicados_detectados.similitud is
    '0-100 — para texto_similar, similitud de texto (rapidfuzz); para fingerprint_similar, similitud acústica (ver app/duplicados.py::_similitud_audio). fingerprint_exacto (legacy) no la usa.';
