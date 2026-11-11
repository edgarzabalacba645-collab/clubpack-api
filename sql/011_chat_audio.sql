-- ------------------------------------------------------------
-- Audio adjunto en el chat de comunidad: permite mandar un mp3/wav/flac
-- junto con (o en vez de) texto — pensado sobre todo para cuando las
-- subidas al catálogo están cerradas para suscriptores (ver
-- sql/010_control_subidas.sql): le mandan el tema al admin por acá y
-- él decide si lo sube.
--
-- El audio en sí vive en R2 (carpeta "chat-audios/", ver
-- storage.subir_archivo_desde_ruta) — acá solo se guarda la referencia.
-- No hace falta tocar las policies de mensajes_chat: el insert/select
-- ya están protegidos por tiene_acceso_activo(), y estas son columnas
-- nuevas nomás sobre la misma fila.
-- ------------------------------------------------------------

alter table public.mensajes_chat
    add column if not exists audio_storage_path text,
    add column if not exists audio_nombre_original text,
    add column if not exists audio_tamano_mb numeric(10,2),
    add column if not exists audio_duracion_seg integer;

comment on column public.mensajes_chat.audio_storage_path is
    'Key en R2 del audio adjunto (mp3/wav/flac), si el mensaje tiene uno. La sube el backend (POST /chat/audio) — nunca llega la key del cliente sin pasar por ahí.';
