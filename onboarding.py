"""
onboarding.py — Flujo de alta de nuevos usuarios (WhatsApp).

6 preguntas estáticas y predefinidas:
  1. Nombre
  2. Cantidad de personas (botones: Solo yo / Somos 2)
  3. Nombre de la segunda persona (si aplica)
  4. Tarjetas de crédito (lista numerada, el usuario escribe los números)
  5. Email de Gmail (para compartir la planilla)
  6. Ingreso mensual estimado (opcional)

Al finalizar: crea la planilla en Google Sheets, la comparte con el email,
registra al usuario como activo.
"""

import logging
import re

import user_store
import sheets as sheets_module
import whatsapp

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

WELCOME_TEXT = (
    "¡Hola! Soy *Axon Finance*, tu asistente financiero personal. 💰\n\n"
    "Entiendo español rioplatense y registro tus gastos e ingresos "
    "con lenguaje natural — sin formularios ni planillas complicadas.\n\n"
    "Voy a hacerte *6 preguntas rápidas* para configurar tu planilla "
    "y arrancamos.\n\n"
    "*Pregunta 1 de 6:* ¿Cómo te llamás?"
)


def _parse_bank_selection(text: str) -> list[str] | None:
    """
    Parsea la selección de bancos del usuario.
    Acepta: "1,3" / "1 3" / "santander galicia" / "0" / "ninguna"
    Retorna lista de bancos seleccionados, o None si no entiende.
    """
    text = text.strip().lower()
    if text in ("0", "ninguna", "ninguno", "no", "n/a", "-", "sin tarjetas", "sin tarjeta"):
        return []

    selected = []
    # Intentar parsear números
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

    # Intentar parsear nombres
    for bank in BANKS:
        if bank.lower() in text:
            if bank not in selected:
                selected.append(bank)
    if selected:
        return selected

    return None  # No se pudo parsear


async def start_onboarding(phone: str) -> None:
    """Inicia el flujo de onboarding para un usuario nuevo."""
    user_store.save_user(phone, {
        "onboarding_state": "WAIT_NAME",
        "onboarding_data": {},
        "setup_complete": False,
    })
    await whatsapp.send_text(phone, WELCOME_TEXT)


async def handle_message(phone: str, text: str) -> bool:
    """
    Procesa mensajes de texto durante el onboarding.
    Retorna True si el mensaje fue consumido por el onboarding.
    """
    user = user_store.get_user(phone)
    if not user or user.get("setup_complete"):
        return False

    state = user.get("onboarding_state")
    odata = user.get("onboarding_data", {})

    if state == "WAIT_NAME":
        name = text.strip()
        if not name or len(name) > 50:
            await whatsapp.send_text(phone, "Por favor escribí tu nombre (máximo 50 caracteres).")
            return True
        odata["nombre"] = name
        user_store.update_user(phone, onboarding_state="WAIT_PERSONAS_COUNT", onboarding_data=odata)
        await whatsapp.send_buttons(
            phone,
            f"¡Buenísimo, {name}! 👋\n\n*Pregunta 2 de 6:* ¿Cuántas personas van a registrar gastos en la planilla?",
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
            f"*Pregunta 4 de 6:* {BANKS_MENU}"
        )
        return True

    elif state == "WAIT_TARJETAS":
        selected = _parse_bank_selection(text)
        if selected is None:
            await whatsapp.send_text(
                phone,
                "No entendí bien. " + BANKS_MENU
            )
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

    elif state == "WAIT_EMAIL":
        email = text.strip().lower()
        if not re.match(r'^[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}$', email):
            await whatsapp.send_text(
                phone,
                "Ese email no parece válido. Ingresá tu email completo (ej: nombre@gmail.com)."
            )
            return True
        odata["email"] = email
        user_store.update_user(phone, onboarding_state="WAIT_INCOME", onboarding_data=odata)
        await whatsapp.send_text(
            phone,
            "*Pregunta 6 de 6:* ¿Cuánto ganás aproximadamente por mes (en pesos)?\n\n"
            "Sirve para mostrarte alertas cuando te estés pasando del presupuesto. "
            'Podés escribir el monto o "Omitir" si preferís no decir.'
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
    """
    Procesa respuestas de botones interactivos durante el onboarding.
    Retorna True si el callback fue consumido.
    """
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
                f"*Pregunta 3 de 6 — Saltada* (solo una persona).\n\n"
                f"*Pregunta 4 de 6:* {BANKS_MENU}"
            )
            return True

        elif button_id == "ob_personas_2":
            odata["personas"] = [nombre]
            user_store.update_user(phone, onboarding_state="WAIT_PERSONA2_NAME", onboarding_data=odata)
            await whatsapp.send_text(
                phone,
                "👥 ¡Buenísimo! Van a ser dos.\n\n"
                "*Pregunta 3 de 6:* ¿Cómo se llama la otra persona?"
            )
            return True

    elif state == "WAIT_TARJETAS_CONFIRM":
        if button_id == "ob_tarjetas_ok":
            user_store.update_user(phone, onboarding_state="WAIT_EMAIL", onboarding_data=odata)
            tarjetas_str = ", ".join(odata.get("tarjetas", [])) or "Sin tarjetas"
            await whatsapp.send_text(
                phone,
                f"✅ Tarjetas guardadas: *{tarjetas_str}*\n\n"
                "*Pregunta 5 de 6:* ¿Cuál es tu email de Gmail?\n"
                "Te voy a compartir la planilla ahí para que puedas verla cuando quieras."
            )
            return True

        elif button_id == "ob_tarjetas_cambiar":
            user_store.update_user(phone, onboarding_state="WAIT_TARJETAS", onboarding_data=odata)
            await whatsapp.send_text(phone, BANKS_MENU)
            return True

    return False


async def _finalize_onboarding(phone: str) -> None:
    """Crea la planilla, guarda el usuario y envía el mensaje de bienvenida final."""
    user = user_store.get_user(phone)
    odata = user.get("onboarding_data", {})

    nombre = odata.get("nombre", "Usuario")
    personas = odata.get("personas", [nombre])
    tarjetas = odata.get("tarjetas", [])
    email = odata.get("email", "")
    ingreso = odata.get("ingreso_estimado")

    await whatsapp.send_text(phone, "⏳ Creando tu planilla de Google Sheets...")

    try:
        sheet_id, sheet_url = sheets_module.create_user_spreadsheet(
            owner_name=nombre,
            owner_email=email or None,
        )
    except Exception as e:
        logger.error("Error creando planilla para phone=%s: %s", phone, e, exc_info=True)
        await whatsapp.send_text(
            phone,
            "❌ No pude crear la planilla automáticamente.\n\n"
            "Puede ser un problema de permisos en Google Cloud. "
            "Contactá al soporte o intentá de nuevo mandando /start."
        )
        user_store.update_user(phone, onboarding_state="WAIT_EMAIL", onboarding_data=odata)
        return

    user_store.save_user(phone, {
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
        f"🎉 *¡Listo, {nombre}! Tu planilla ya está configurada.*\n\n"
        f"👤 Personas: {personas_str}\n"
        f"💳 Tarjetas: {tarjetas_str}\n"
        f"💰 Ingreso estimado: {ingreso_str}\n\n"
        f"📊 Tu planilla de Google Sheets:\n{sheet_url}\n\n"
        "Ya podés empezar a usarme. Algunos ejemplos:\n"
        '• _"Gasté $5.000 en el super"_\n'
        '• _"Cobré $500.000 de sueldo"_\n'
        '• _"¿Cuánto gasté este mes?"_\n'
        '• _"Pagué el alquiler $200.000"_\n\n'
        "Mandá *ayuda* para ver todo lo que puedo hacer. 🚀"
    )
