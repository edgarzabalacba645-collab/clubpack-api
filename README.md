# ClubPack API

Backend propio de ClubPack, en FastAPI. Hace solo lo que el navegador
no puede (o no debe) hacer:

- Transcodificar cualquier audio subido a **MP3 320kbps** (ffmpeg).
- Detectar **BPM** (librosa) y **fingerprint de audio** (fpcalc/Chromaprint)
  para encontrar duplicados.
- Guardar y leer los archivos en **Cloudflare R2** (acá viven las claves
  secretas — nunca deben llegar al navegador).
- Firmar URLs de descarga de **Cloudflare R2** para selecciones o playlists
  enteras (varios tracks de una, sin armar ningún ZIP — ver más abajo).
- Correr el **escaneo de duplicados** de todo el catálogo (comparación
  texto + fingerprint, pesada para hacerla en el navegador).

Todo lo demás (login, ver el catálogo, administrar géneros y usuarios,
Top 50) lo hace el frontend React hablando **directo** con Supabase —
no pasa por acá, porque no necesita ni ffmpeg ni claves secretas, y las
políticas RLS de `sql/schema.sql` ya controlan los permisos.

## Cómo funciona la autenticación

El frontend hace login contra Supabase Auth y, en cada llamada a esta
API, manda el access token del usuario en el header
`Authorization: Bearer <token>`. Este backend valida ese token contra
Supabase y arma un cliente que opera **como ese usuario** — nunca usa
la `service_role key`. Así las políticas RLS siguen siendo la única
fuente de verdad: si un usuario no tiene suscripción activa o no es
admin, la base de datos misma rechaza la operación aunque alguien
intente llamar a la API directamente.

## Instalar y correr en local

Requiere Python 3.11+ y, para transcodificar/detectar BPM/fingerprint
de verdad (si no están, cada función degrada sola y devuelve `None` /
el archivo sin convertir, para no romper el flujo):

```bash
# Ubuntu/Debian/WSL
sudo apt update && sudo apt install -y $(cat packages-sistema.txt | grep -v '^#')

# macOS (Homebrew)
brew install ffmpeg chromaprint
```

Si tu base de Supabase ya tenía `schema.sql` y `002_mejoras.sql`
corridos de antes, falta sumarle **`sql/003_pagos.sql`** (Supabase >
SQL Editor > pegar y correr) — agrega la configuración de suscripción,
los campos nuevos de `pagos` y la función del webhook. Al final de ese
mismo archivo hay un bloque comentado para generar y guardar el
secreto interno del webhook (`openssl rand -hex 32`); sin ese paso el
webhook de Mercado Pago no va a poder activar ninguna suscripción.

Después:

```bash
python3 -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# completá .env con la MISMA Supabase URL/anon key que ya tenés en
# .streamlit/secrets.toml, y las claves de R2 (Mercado Pago es opcional,
# ver la sección "Suscripción paga" más abajo)

uvicorn app.main:app --reload --port 8000
```

Con el server corriendo, entrá a **http://localhost:8000/docs** — ahí
podés probar cada endpoint a mano desde el navegador (Swagger UI),
pegando un access token real de Supabase para probar como un usuario
de verdad.

El frontend React (`clubpack-web/.env`, variable `VITE_API_BASE_URL`)
tiene que apuntar a esta misma URL.

## Endpoints

| Método | Ruta | Para qué | Requiere |
|---|---|---|---|
| POST | `/tracks/analyze` | Transcodifica, detecta BPM/duración/fingerprint, chequea duplicados por texto | Puede subir (suscripción activa, y además habilitado por el admin — ver sql/010_control_subidas.sql) |
| POST | `/tracks/confirm` | Sube a R2 y crea el track en el catálogo | Puede subir (ídem) |
| GET | `/tracks/{id}/download-url` | URL firmada para bajar (`registrar=true`, default) o solo previsualizar (`registrar=false`, lo usa el player del catálogo) un track | Bajar: suscripción activa siempre. Previsualizar: suscripción activa, o el track está en la muestra gratuita de `obtener_muestra_gratis_cachengue()` (ver sql/012_freemium_v2_vidriera.sql) |
| POST | `/tracks/zip` | URL firmada por track de una selección, para bajarlos directo de R2 (loguea cada descarga) — el nombre quedó de cuando armaba un ZIP de verdad, ver nota abajo | Suscripción activa |
| POST | `/playlists/{id}/zip` | Lo mismo para una playlist completa | Suscripción activa |
| POST | `/chat/audio` | Sube un mp3/wav/flac a R2 (sin transcodificar) para compartir en el chat | Suscripción activa |
| GET | `/chat/mensajes/{id}/audio-url` | URL firmada para escuchar/bajar el audio adjunto a un mensaje del chat | Suscripción activa (y poder ver ese mensaje, vía RLS) |
| DELETE | `/chat/mensajes` | Vacía todo el historial del chat (mensajes + sus audios en R2) | Admin |
| POST | `/chat/mensajes/{id}/analizar` | Puente: analiza un audio ya compartido en el chat y lo deja en espera, igual que `/tracks/analyze` pero sin bajarlo/resubirlo a mano | Admin |
| POST | `/admin/duplicados/scan` | Escanea todo el catálogo buscando duplicados | Admin |
| GET | `/admin/duplicados` | Lista los duplicados pendientes de revisión | Admin |
| PATCH | `/admin/duplicados/{id}` | Marca un duplicado como descartado/resuelto | Admin |
| DELETE | `/tracks/{id}` | Borra un track (archivo en R2 + estado='eliminado') | Admin |
| POST | `/suscripcion/mercadopago/crear` | Crea la suscripción recurrente en Mercado Pago, devuelve el link de pago (`init_point`) | Login (no necesita suscripción activa) |
| POST | `/suscripcion/mercadopago/webhook` | Recibe las notificaciones de Mercado Pago y activa/actualiza la suscripción | Público — se protege validando la firma `x-signature` de MP, no con login |

El `DELETE /tracks/{id}` existe por la misma razón que el resto de
este backend: borrar el archivo de R2 necesita las credenciales del
bucket, que el frontend nunca tiene. Lo usa el panel de Admin al
resolver un duplicado ("Borrar éste"). No es un delete duro — deja la
fila en `tracks` con `estado='eliminado'` para no perder el historial
de descargas/duplicados asociado a ese track.

**Sobre `/tracks/zip` y `/playlists/{id}/zip`**: originalmente bajaban
cada archivo de R2 a la memoria del backend y armaban un ZIP ahí mismo
antes de mandarlo — con el plan gratis de Render (512MB) varias
selecciones grandes pedidas en simultáneo podían sumar más memoria de
la que hay disponible. Ahora devuelven una URL firmada por track (el
mismo mecanismo que `/tracks/{id}/download-url`) y el navegador baja
cada archivo directo de R2, uno tras otro — cero bytes de audio pasan
por este servidor. El nombre de la ruta quedó igual por no romper el
frontend sin necesidad; lo que cambió es la respuesta (antes el ZIP en
sí, ahora la lista de URLs).

### Suscripción paga (Mercado Pago + transferencia manual)

El grupo puede cobrar la suscripción mensual de dos formas, las dos
habilitables/deshabilitables desde Admin > Suscripción en el
frontend, sin tocar código:

- **Mercado Pago (automático)**: pasa por este backend porque crear
  la suscripción y recibir el webhook necesita el Access Token de
  Mercado Pago, que es un secreto tan sensible como el de R2. Todo lo
  demás (configurar el precio, el CBU/CVU, revisar transferencias
  manuales) es Supabase directo desde el frontend, protegido por RLS.
- **Transferencia manual (CBU/CVU/alias)**: NO pasa por este backend
  en absoluto. El usuario inserta directo en Supabase su propio aviso
  de pago (`pagos`, `tipo='manual'`, `estado='pendiente'`) — hay una
  policy de RLS (`avisar_transferencia_manual_propia`, en
  `sql/003_pagos.sql`) que permite exactamente eso y nada más. El
  admin lo aprueba o rechaza también directo contra Supabase (ya
  puede actualizar cualquier `perfiles`/`pagos` por ser admin). No
  hizo falta un endpoint nuevo para esto.

**Cómo funciona el flujo de Mercado Pago:**

1. El usuario entra a "Suscripción" y aprieta "Pagar con Mercado
   Pago". El frontend llama a `POST /suscripcion/mercadopago/crear`,
   que lee el precio configurado (`configuracion_suscripcion`), crea
   un "Preapproval" (suscripción recurrente sin plan asociado) en la
   API de Mercado Pago, y devuelve `init_point` — un link donde el
   usuario autoriza el cobro mensual con su tarjeta, del lado de
   Mercado Pago. El `external_reference` de esa suscripción queda en
   el id de usuario de Supabase, para poder identificarlo después.
2. El frontend muestra ese link como botón ("Pagar con Mercado
   Pago") y como QR (el QR es ese mismo link, no hace falta nada más
   para generarlo).
3. Cuando el usuario autoriza el pago, Mercado Pago le pega a
   `POST /suscripcion/mercadopago/webhook` (tenés que configurar esa
   URL completa en Tus integraciones > Webhooks). Este backend valida
   la firma `x-signature` del aviso, busca el estado real de la
   suscripción en la API de Mercado Pago (nunca confía en el body del
   webhook a ciegas) y activa al usuario.
4. Activar la suscripción de OTRO usuario (no de quien está llamando)
   es la única operación de todo este proyecto que no puede hacerse
   con el token de un usuario normal ni de un admin — el webhook de
   Mercado Pago no tiene sesión de la app. Para esto **no se usa la
   service_role key** (rompería la regla de todo el proyecto); en su
   lugar hay una función SQL puntual, `procesar_notificacion_mp` (ver
   `sql/003_pagos.sql`), que exige su propio secreto
   (`BACKEND_INTERNAL_SECRET`, generado por vos, sin relación con
   Mercado Pago ni con Supabase) antes de escribir nada. Si el .env de
   este backend se filtrara, el daño queda acotado a esa única función
   puntual, no a un bypass total de RLS.

**Importante para probar esto de verdad:** la validación de
`x-signature` (`app/mercadopago.py::validar_firma_webhook`) se armó
con la mejor referencia disponible de Mercado Pago, pero su
documentación pública no muestra el algoritmo paso a paso en la
pestaña "Sin SDKs" (esa parte de la página necesita JavaScript para
verse). Antes de depender de esto en producción, probalo con el botón
"Simular notificación" del panel de Webhooks de Mercado Pago — si
rechaza siempre una notificación simulada real, lo primero a revisar
es el formato exacto del *manifest* (la línea `id:...;request-id:...;ts:...;`
que se firma) contra lo que Mercado Pago realmente usa.

### El flujo de subida en 2 pasos

Igual que en la versión Streamlit: primero se analiza, después se
confirma — así el usuario ve BPM/duración/avisos de duplicado antes
de subir de verdad.

1. `POST /tracks/analyze` (multipart: `file`, `titulo`, `remixer`) →
   transcodifica y analiza, deja el MP3 ya convertido esperando en
   disco (ver `app/staging.py`), devuelve `staging_id` + métricas +
   posibles duplicados.
2. `POST /tracks/confirm` (JSON con `staging_id` + resto de los
   metadatos del track) → recién ahí sube el archivo a R2 e inserta
   la fila en `tracks`.

El archivo en espera vive en disco (no en memoria del proceso, ni en
una cola compartida) y vence a los 90 minutos (`STAGING_TTL_SEG`) si
nadie lo confirma. Para 20-30 DJs y un servidor con un solo worker
alcanza de sobra; si en algún momento este backend corre con varias
réplicas, esa espera tiene que moverse a algo compartido (Redis, un
bucket temporal en R2, etc.) porque cada proceso tendría su propio
directorio de espera y no se verían entre sí.

## Deploy

Esta API necesita un servidor de verdad (no serverless clásico) porque
usa ffmpeg/fpcalc/librosa como binarios del sistema y guarda estado
en memoria durante la subida. Opciones simples:

- **VPS chico** (Hetzner, DigitalOcean, etc. — US$6-12/mes): instalar
  `packages-sistema.txt`, clonar, `pip install -r requirements.txt`,
  correr con `uvicorn` detrás de un proceso supervisado (systemd,
  `pm2`, o un contenedor Docker) y Nginx/Caddy delante para HTTPS.
- **Render / Railway**: servicio de tipo "Web Service" con un
  Dockerfile que instale `packages-sistema.txt` vía `apt-get` — el
  plan gratis de Render se duerme, pero no tiene la limitación de
  1-app-privada que tenía Streamlit Community Cloud.

El frontend (React) se despliega aparte, como sitio estático
(Vercel/Netlify/Cloudflare Pages), y apunta a la URL de esta API vía
`VITE_API_BASE_URL`.

## Variables de entorno

Ver `.env.example`. Nunca se sube `.env` a git (`.gitignore` ya lo
excluye). A diferencia del `.env` del frontend, acá SÍ pueden vivir
claves secretas (R2 secret) porque este proceso corre en un servidor,
no en el navegador del usuario.
