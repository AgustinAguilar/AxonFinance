import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# WhatsApp / YCloud API
YCLOUD_API_KEY = os.getenv("YCLOUD_API_KEY")
YCLOUD_PHONE_NUMBER = os.getenv("YCLOUD_PHONE_NUMBER")  # ej: "+5491157501453"
YCLOUD_WEBHOOK_SECRET = os.getenv("YCLOUD_WEBHOOK_SECRET", "")
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

# Gemini (transcripción de audio)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# Google OAuth 2.0 (cada usuario conecta su propio Drive)
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "https://finance.axonlab.cloud/auth/callback",
)
GOOGLE_OAUTH_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.file",
]

# Secret para firmar el state de OAuth (HMAC)
STATE_SECRET = os.getenv("STATE_SECRET", "axon-finance-state-dev-change-me")

TAB_GASTOS = "Gastos"
TAB_INGRESOS = "Ingresos"
TAB_TARJETAS = "Tarjetas"
TAB_RESUMEN = "Resumen"
TAB_AHORROS = "Ahorros"
TAB_GASTOS_FIJOS = "Gastos Fijos"
TAB_PRESTAMOS = "Prestamos"
TAB_HISTORICO = "Historico"

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

# Bancos soportados para importar resúmenes de tarjeta en PDF
BANKS_SUPPORTED = ["Santander", "BBVA", "Galicia", "ICBC"]

HEADERS_GASTOS = [
    "id", "fecha", "persona", "descripcion", "monto",
    "metodo_pago", "tarjeta", "cuotas_total", "cuota_actual",
    "monto_cuota", "origen", "categoria",
]
HEADERS_INGRESOS = ["id", "fecha", "persona", "tipo", "monto", "descripcion"]
HEADERS_TARJETAS = ["descripcion", "tarjeta", "persona", "cuota_actual", "cuotas_total", "monto_cuota"]
HEADERS_RESUMEN = ["mes", "ingreso_total", "gastos_total", "ahorro", "saldo"]
HEADERS_AHORROS = ["fecha", "persona", "tipo", "monto_ars", "cotizacion_blue", "monto_usd", "notas"]
HEADERS_GASTOS_FIJOS = [
    "nombre", "monto_estimado", "metodo_pago", "tarjeta",
    "origen", "activo", "descripcion_tarjeta", "persona",
]
HEADERS_PRESTAMOS = ["id", "fecha", "persona", "contraparte", "tipo", "monto", "moneda", "notas"]
HEADERS_HISTORICO = ["año", "mes"] + [
    "id", "fecha", "persona", "descripcion", "monto",
    "metodo_pago", "tarjeta", "cuotas_total", "cuota_actual",
    "monto_cuota", "origen", "categoria",
]

TAB_HEADERS = {
    TAB_GASTOS: HEADERS_GASTOS,
    TAB_INGRESOS: HEADERS_INGRESOS,
    TAB_TARJETAS: HEADERS_TARJETAS,
    TAB_RESUMEN: HEADERS_RESUMEN,
    TAB_AHORROS: HEADERS_AHORROS,
    TAB_GASTOS_FIJOS: HEADERS_GASTOS_FIJOS,
    TAB_PRESTAMOS: HEADERS_PRESTAMOS,
    TAB_HISTORICO: HEADERS_HISTORICO,
}

CATEGORIES = [
    "comida", "transporte", "entretenimiento", "salud",
    "educacion", "servicios", "ropa", "hogar", "otros",
]

MAX_HISTORY = 20

SYSTEM_PROMPT_TEMPLATE = """Sos Axon Finance, un asistente financiero personal inteligente y amigable.
La fecha de hoy es {fecha}.
El usuario se llama {nombre}.
Personas registradas en la planilla: {personas}.
Tarjetas de crédito configuradas: {tarjetas}.

Tu trabajo es ayudar a registrar gastos, ingresos, y consultar la situación financiera del usuario.
Todo se guarda en una planilla de Google Sheets personal.

Reglas:
- Cuando el usuario menciona un gasto, usá add_expense. Inferí el método de pago del contexto:
  "débito"/"transferencia"/"mp"/"mercado pago" → mercadopago,
  "tarjeta" → tarjeta_credito, "efectivo" → efectivo,
  "cuotas" → tarjeta_credito, "mercado crédito" → mercado_credito.
- Si es tarjeta_credito y no especifica cuál, preguntá cuál de sus tarjetas ({tarjetas}).
  Si no tiene tarjetas configuradas, registrá como tarjeta_credito genérica.
- Si no especifica fecha, usá hoy.
- Si no especifica cuotas para tarjeta, asumí 1 cuota (pago completo).
- Cuando muestres montos, usá formato argentino: $30.000
- Respondé siempre en español argentino, conciso y amigable.
- Si el usuario envía un PDF, preguntá de qué banco es el resumen y usá parse_credit_card_pdf.
- Gastos fijos: usá pay_fixed_expense cuando digan que pagaron alquiler, luz, gas, internet, etc.
  Si mencionan un monto nuevo, pasalo para actualizar el catálogo.
  Para gastos fijos de tarjeta (streaming, etc.) NO usar pay_fixed_expense; vienen del PDF.
- Inferí la categoría automáticamente según descripción:
  super/almacén/restaurante/delivery → comida, uber/combustible/estacionamiento → transporte,
  netflix/spotify/cine/juegos → entretenimiento, farmacia/médico/clínica → salud,
  curso/libro/universidad → educacion, luz/gas/internet/alquiler → servicios,
  ropa/zapatillas/indumentaria → ropa, muebles/electro/ferretería → hogar. Si no → otros.
- Préstamos (NO afectan gastos ni ingresos): usá add_loan.
  "le presté X a Y" → prestamo_dado, "Y me prestó X" → prestamo_recibido,
  "Y me devolvió X" → devolucion_recibida, "le devolví X a Y" → devolucion_dada.
- Podés llamar múltiples tools si hace falta para responder una consulta.
- Si hay varias personas en la planilla y no especifica, asumí {nombre} como persona activa."""
