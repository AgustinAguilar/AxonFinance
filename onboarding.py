"""
onboarding.py — Flujo de alta de nuevos usuarios (WhatsApp).

Estados:
  WAIT_OAUTH         → esperando que el cliente conecte Google Drive
  WAIT_NAME          → (disparado desde el callback de OAuth)
  WAIT_PERSONAS_COUNT
  WAIT_PERSONA2_NAME
  WAIT_TARJETAS
  WAIT_TARJETAS_CONFIRM
  WAIT_INCOME
  CREATING_SHEET
  DONE
"""

import logging
import re

import user_store
import sheets as sheets_module
import whatsapp
from config import GOOGLE_OAUTH_REDIRECT_URI

logger = logging.getLogger(__name__)

BANKS = ["Santander", "BBVA", "Galicia", "ICBC"]

BANKS_MENU = (
    "¿Qué tarjetas de crédito usás? Escribí los números separados por coma "
    "(o *0* si no tenés ninguna):\n\n"
    "1. Santander\n"
    "2. BBVA\n"
    "3. Galicia\n"
    "4. ICBC\n\n"
    "_Ejemplo: 1,3 para Santander y Galicia_"
)


def _auth_link_for_phone(phone: str) -> str:
    """Construye el link público de OAuth para el cliente."""
    # GOOGLE_OAUTH_REDIRECT_URI apunta a /auth/callback → reemplazamos para armar /auth/{phone}
    base = GOOGLE_OAUTH_REDIRECT_URI.rsplit("/auth/", 1)[0]
    return f"{base}/auth/{phone}"


WELCOME_TEXT_TEMPLATE = (
    "¡Hola! Soy *Axon Finance*, tu asistente financiero personal. 💰\n\n"
    "Entiendo español rioplatense y registro tus gastos e ingresos "
    "con lenguaje natural — sin formularios ni planillas complicadas.\n\n"
    "*Paso 1 de 2:* Conectá tu Google Drive\n"
    "Tu planilla se va a crear en *tu propio Drive*, con tu cuenta. "
    "Nosotros no vemos tus datos — la app solo accede al archivo que ella misma crea.\n\n"
    "👉 Tocá este link y firmá con Google:\n"
    "{link}\n\n"
    "Cuando termines, volvé acá y seguimos. 🚀"
)


def _parse_bank_selection(text: str) -> list[str] | None:
    """
    Parsea la selección de bancos del usuario.
    Acepta: "1,3" / "1 3" / "santander galicia" / "0" / "ninguna"
    """
    text = text.strip().lower()
    if text in ("0", "ninguna", "ninguno", "no", "n/a", "-", "sin tarjetas", "sin tarjeta"):
        return []

    selected = []
    numbers = re.findall(r'\d+', text)
    if numbers:
        for n in numbers:
            idx = int(n) - 1
            if 0 <= idx < len(BANKS):
                bank = BANKS[idx]
                if bank not in selected:
                    selected.append(bank)
        if selected:
            return selected

    for bank in BANKS:
        if bank.lower() in text:
            if bank not in selected:
                selected.append(bank)
    if selected:
        return selected

    return None


async def start_onboarding(phone: str) -> None:
    """Inicia el onboarding: pide conexión OAuth con Google."""
    user_store.save_user(phone, {
        "onboarding_state": "WAIT_OAUTH",
        "onboarding_data": {},
        "setup_complete": False,
    })
    link = _auth_link_for_phone(phone)
    await whatsapp.send_text(phone, WELCOME_TEXT_TEMPLATE.format(link=link))


async def handle_message(phone: str, text: str) -> bool:
    """Procesa mensajes de texto durante el onboarding. Retorna True si lo consume."""
    user = user_store.get_user(phone)
    if not user or user.get("setup_complete"):
        return False

    state = user.get("onboarding_state")
    odata = user.get("onboarding_data", {})

    if state == "WAIT_OAUTH":
        # El usuario está escribiendo antes de conectar Google — recordatorio.
        link = _auth_link_for_phone(phone)
        await whatsapp.send_text(
            phone,
            "Primero necesito que conectes tu Google Drive.\n\n"
            f"👉 {link}\n\n"
            "Cuando termines, seguimos con las preguntas."
        )
        return True

    if state == "WAIT_NAME":
        name = text.strip()
        if not name or len(name) > 50:
            await whatsapp.send_text(phone, "Por favor escribí tu nombre (máximo 50 caracteres).")
            return True
        odata["nombre"] = name
        user_store.update_user(phone, onboarding_state="WAIT_PERSONAS_COUNT", onboarding_data=odata)
        await whatsapp.send_buttons(
            phone,
            f"¡Buenísimo, {name}! 👋\n\n*Pregunta 2 de 5:* ¿Cuántas personas van a registrar gastos en la planilla?",
            [
                {"id": "ob_personas_1", "title": "Solo yo"},
                {"id": "ob_personas_2", "title": "Somos 2"},
            ],
        )
        return True

    elif state == "WAIT_PERSONA2_NAME":
        name2 = text.strip()
        if not name2 or len(name2) > 50:
            await whatsapp.send_text(phone, "Por favor escribí el nombre (máximo 50 caracteres).")
            return True
        personas = odata.get("personas", [odata.get("nombre", "Usuario")])
        personas.append(name2)
        odata["personas"] = personas
        user_store.update_user(phone, onboarding_state="WAIT_TARJETAS", onboarding_data=odata)
        await whatsapp.send_text(
            phone,
            f"Perfecto, registramos a *{personas[0]}* y *{name2}* 👫\n\n"
            f"*Pregunta 4 de 5:* {BANKS_MENU}"
        )
        return True

    elif state == "WAIT_TARJETAS":
        selected = _parse_bank_selection(text)
        if selected is None:
            await whatsapp.send_text(phone, "No entendí bien. " + BANKS_MENU)
            return True

        odata["tarjetas"] = selected
        tarjetas_str = ", ".join(selected) if selected else "Sin tarjetas"
        user_store.update_user(phone, onboarding_state="WAIT_TARJETAS_CONFIRM", onboarding_data=odata)

        await whatsapp.send_buttons(
            phone,
            f"Seleccionaste: *{tarjetas_str}*\n¿Confirmás?",
            [
                {"id": "ob_tarjetas_ok", "title": "Sí, confirmar"},
                {"id": "ob_tarjetas_cambiar", "title": "Cambiar"},
            ],
        )
        return True

    elif state == "WAIT_INCOME":
        texto = text.strip().lower()
        if texto in ("omitir", "no", "n/a", "-", "no sé", "no se", "paso", "saltar"):
            odata["ingreso_estimado"] = None
        else:
            raw = texto.replace(".", "").replace(",", "").replace("$", "").replace(" ", "")
            try:
                odata["ingreso_estimado"] = int(raw)
            except ValueError:
                await whatsapp.send_text(phone, 'Escribí un número (ej: 500000) o "Omitir" para saltear.')
                return True

        user_store.update_user(phone, onboarding_state="CREATING_SHEET", onboarding_data=odata)
        await _finalize_onboarding(phone)
        return True

    return False


async def handle_button_reply(phone: str, button_id: str, button_title: str) -> bool:
    """Procesa respuestas de botones durante el onboarding."""
    user = user_store.get_user(phone)
    if not user or user.get("setup_complete"):
        return False

    if not button_id.startswith("ob_"):
        return False

    state = user.get("onboarding_state")
    odata = user.get("onboarding_data", {})

    if state == "WAIT_PERSONAS_COUNT":
        nombre = odata.get("nombre", "Usuario")
        if button_id == "ob_personas_1":
            odata["personas"] = [nombre]
            user_store.update_user(phone, onboarding_state="WAIT_TARJETAS", onboarding_data=odata)
            await whatsapp.send_text(
                phone,
                f"👤 Solo *{nombre}*. Perfecto.\n\n"
                f"*Pregunta 3 de 5 — Saltada* (solo una persona).\n\n"
                f"*Pregunta 4 de 5:* {BANKS_MENU}"
            )
            return True

        elif button_id == "ob_personas_2":
            odata["personas"] = [nombre]
            user_store.update_user(phone, onboarding_state="WAIT_PERSONA2_NAME", onboarding_data=odata)
            await whatsapp.send_text(
                phone,
                "👥 ¡Buenísimo! Van a ser dos.\n\n"
                "*Pregunta 3 de 5:* ¿Cómo se llama la otra persona?"
            )
            return True

    elif state == "WAIT_TARJETAS_CONFIRM":
        if button_id == "ob_tarjetas_ok":
            user_store.update_user(phone, onboarding_state="WAIT_INCOME", onboarding_data=odata)
            tarjetas_str = ", ".join(odata.get("tarjetas", [])) or "Sin tarjetas"
            await whatsapp.send_text(
                phone,
                f"✅ Tarjetas guardadas: *{tarjetas_str}*\n\n"
                "*Pregunta 5 de 5:* ¿Cuánto ganás aproximadamente por mes (en pesos)?\n\n"
                "Sirve para mostrarte alertas cuando te estés pasando del presupuesto. "
                'Podés escribir el monto o "Omitir" si preferís no decir.'
            )
            return True

        elif button_id == "ob_tarjetas_cambiar":
            user_store.update_user(phone, onboarding_state="WAIT_TARJETAS", onboarding_data=odata)
            await whatsapp.send_text(phone, BANKS_MENU)
            return True

    return False


async def _finalize_onboarding(phone: str) -> None:
    """Crea la planilla en el Drive del cliente y envía el mensaje final."""
    user = user_store.get_user(phone)
    odata = user.get("onboarding_data", {})

    nombre = odata.get("nombre", "Usuario")
    personas = odata.get("personas", [nombre])
    tarjetas = odata.get("tarjetas", [])
    email = odata.get("email", user.get("oauth", {}).get("google_email", ""))
    ingreso = odata.get("ingreso_estimado")

    await whatsapp.send_text(phone, "⏳ Creando tu planilla en tu Drive...")

    try:
        sheet_id, sheet_url = sheets_module.create_user_spreadsheet_for_phone(
            phone=phone,
            owner_name=nombre,
        )
    except Exception as e:
        logger.error("Error creando planilla para phone=%s: %s", phone, e, exc_info=True)
        await whatsapp.send_text(
            phone,
            "❌ No pude crear la planilla. Puede ser un problema con tu conexión a Google.\n\n"
            "Mandá *inicio* para volver a probar."
        )
        user_store.update_user(phone, onboarding_state="WAIT_INCOME", onboarding_data=odata)
        return

    user_store.save_user(phone, {
        **user,
        "nombre": nombre,
        "email": email,
        "personas": personas,
        "tarjetas": tarjetas,
        "ingreso_estimado": ingreso,
        "sheet_id": sheet_id,
        "sheet_url": sheet_url,
        "setup_complete": True,
        "onboarding_state": "DONE",
        "onboarding_data": {},
    })

    tarjetas_str = ", ".join(tarjetas) if tarjetas else "Ninguna"
    personas_str = " y ".join(personas)
    ingreso_str = f"${ingreso:,}".replace(",", ".") if ingreso else "No especificado"

    await whatsapp.send_text(
        phone,
        f"🎉 *¡Listo, {nombre}! Tu planilla ya está en tu Drive.*\n\n"
        f"👤 Personas: {personas_str}\n"
        f"💳 Tarjetas: {tarjetas_str}\n"
        f"💰 Ingreso estimado: {ingreso_str}\n\n"
        f"📊 Abrila desde tu Drive o acá:\n{sheet_url}\n\n"
        "Ya podés empezar a usarme. Ejemplos:\n"
        '• _"Gasté $5.000 en el super"_\n'
        '• _"Cobré $500.000 de sueldo"_\n'
        '• _"¿Cuánto gasté este mes?"_\n\n'
        "Mandá *ayuda* para ver todo. 🚀"
    )
