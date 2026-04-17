# Setup WhatsApp — Axon Finance

## Parte 1 — Número de teléfono

Necesitás un número que NO esté registrado actualmente en WhatsApp.

**Opción A (recomendada): SIM prepaga nueva**
- Comprá una SIM prepaga (Claro/Personal/Movistar) — ~$1000 ARS
- Activala con crédito mínimo
- No la registres en WhatsApp — dejala limpia
- La vas a necesitar para recibir un SMS de verificación de Meta (una sola vez)

**Opción B: Twilio (sin SIM física)**
- Crear cuenta en twilio.com
- Comprar un número (+1 USA) por ~$1.15/mes
- Desde el panel de Twilio podés recibir el SMS de Meta

---

## Parte 2 — Meta Developer Account

### Paso 1: Crear cuenta de desarrollador
1. Ir a https://developers.facebook.com
2. Loguearte con tu Facebook
3. Aceptar las políticas de desarrollador

### Paso 2: Crear la app
1. Click en **My Apps** → **Create App**
2. Use case: elegir **"Other"** → Next
3. App type: elegir **"Business"** → Next
4. App name: `Axon Finance` (o el nombre que quieras)
5. App contact email: el tuyo
6. Click **Create App**

### Paso 3: Agregar el producto WhatsApp
1. En el dashboard de tu app, buscá el bloque **WhatsApp**
2. Click en **Set up**
3. En *"Step 1 — Select a Meta Business Account"*:
   - Si no tenés una cuenta de negocio, hacé click en "Create a new Meta Business Account"
   - Nombre: `Axon Finance` (o tu nombre de negocio)
   - Click **Continue**
4. En *"Step 2 — Add a phone number"*:
   - Display name: `Axon Finance`
   - Email de negocios: el tuyo
   - Category: `Finance`
   - Click **Next**
   - Ingresá el número de tu SIM/Twilio (con código de país, ej: +54 9 11 1234-5678)
   - Elegí verificar por **SMS**
   - Ingresá el código que te llega → **Verify**

### Paso 4: Obtener las credenciales
Una vez que el número está verificado, en el panel de WhatsApp → **API Setup** vas a ver:

- **Phone Number ID** → lo copiás al `.env` como `WHATSAPP_PHONE_NUMBER_ID`
- El token temporal de 24hs es solo para testear

**Para el token permanente (necesario para producción):**
1. Ir a **Business Settings** (business.facebook.com)
2. En el menú izquierdo → **Users** → **System Users**
3. Click **Add** → crear un System User con rol **Admin**
4. Una vez creado, click en **Generate New Token**
5. Seleccionar tu app
6. Permisos requeridos:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
7. Click **Generate Token**
8. ⚠️ Copialo ahora — no lo vas a poder ver de nuevo
9. Pegarlo en `.env` como `WHATSAPP_TOKEN`

**Para el App Secret:**
1. En tu app → **Settings** → **Basic**
2. Buscar **App Secret** → click en "Show"
3. Copiarlo en `.env` como `WHATSAPP_APP_SECRET`

### Paso 5: Configurar el Webhook (después de deployar el bot)
1. En el panel de tu app → **WhatsApp** → **Configuration**
2. En la sección **Webhook** → click **Edit**
3. Completar:
   - **Callback URL**: `https://tudominio.com/webhook`
   - **Verify token**: el mismo string que pusiste en `.env` como `WEBHOOK_VERIFY_TOKEN`
4. Click **Verify and Save** — el servidor tiene que estar corriendo en ese momento
5. Una vez verificado, en **Webhook fields** → buscar **messages** → click **Subscribe**

---

## Parte 3 — Deploy en el VPS

```bash
# 1. Subir los archivos al VPS
scp -r . usuario@IP_DEL_VPS:/tmp/axon-finance/

# 2. En el VPS, correr el script de deploy
ssh usuario@IP_DEL_VPS
cd /tmp/axon-finance
chmod +x deploy.sh
sudo ./deploy.sh
# El script te va a pedir completar el .env en el camino
```

El script hace automáticamente:
- Instala Python, Nginx, Certbot
- Crea el entorno virtual e instala dependencias
- Configura Nginx como reverse proxy
- Obtiene el certificado SSL (HTTPS) con Let's Encrypt
- Crea el servicio de systemd para que el bot corra siempre

**Comandos útiles en el VPS:**
```bash
# Ver logs en tiempo real
journalctl -u axon-finance -f

# Reiniciar el bot
systemctl restart axon-finance

# Ver estado
systemctl status axon-finance

# Testear que el bot responde
curl https://tudominio.com/health
```

---

## Para testear sin VPS (desarrollo local)

```bash
# Terminal 1 — correr el bot
uvicorn bot:app --reload --port 8000

# Terminal 2 — exponer al exterior con ngrok
ngrok http 8000
# Copiá la URL https://xxxx.ngrok.io y usala como webhook en Meta
```

> ⚠️ ngrok gratuito cambia la URL cada vez que reiniciás. Para desarrollo está bien,
> pero no sirve para producción.

---

## Resumen del .env que necesitás completar

```
ANTHROPIC_API_KEY=sk-ant-...           # de console.anthropic.com
WHATSAPP_TOKEN=EAAxxxxx...             # System User Token de Meta
WHATSAPP_PHONE_NUMBER_ID=123456...     # ID del número en Meta (no el número real)
WHATSAPP_APP_SECRET=abc123...          # App Secret de Meta → Settings → Basic
WEBHOOK_VERIFY_TOKEN=cualquier_string  # Lo elegís vos, usás el mismo en Meta
GOOGLE_CREDENTIALS_FILE=credentials.json
```
