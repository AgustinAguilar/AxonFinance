"""
bot.py — Bot de WhatsApp con FastAPI.

Webhook que recibe mensajes de Meta Cloud API y los procesa con el agente Claude.
Cada usuario es identificado por su número de teléfono (ej: "5491112345678").
"""

import atexit
import glob
import logging
import os
import tempfile
from datetime import datetime

import anthropic
from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS,
    MAX_HISTORY, SYSTEM_PROMPT_TEMPLATE, MESES_ES,
)
import user_store
import sheets
import onboarding
import tools_archive
import tools_dashboard
import whatsapp
from tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

app = FastAPI(title="Axon Finance Bot")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Estado en memoria (keyed por phone number)
conversations: dict[str, list[dict]] = {}
pending_pdfs: dict[str, str] = {}

# Deduplicación de mensajes ya procesados
processed_message_ids: set[str] = set()
MAX_PROCESSED_IDS = 1000  # evitar memory leak


def _cleanup_pending_pdfs():
    for path in list(pending_pdfs.values()):
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(_cleanup_pending_pdfs)


def _get_system_prompt(user: dict) -> str:
    personas = user.get("personas", [user.get("nombre", "Usuario")])
    tarjetas = user.get("tarjetas", [])
    return SYSTEM_PROMPT_TEMPLATE.format(
        fecha=datetime.now().strftime("%Y-%m-%d"),
        nombre=user.get("nombre", "Usuario"),
        personas=", ".join(personas) if personas else "Sin personas configuradas",
        tarjetas=", ".join(tarjetas) if tarjetas else "Sin tarjetas",
    )


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Recibe mensajes de WhatsApp vía YCloud. Responde 200 y procesa en background."""
    body_bytes = await request.body()

    # Verificar firma de YCloud
    sig_header = request.headers.get("YCloud-Signature", "")
    if not whatsapp.verify_signature(body_bytes, sig_header):
        logger.warning("Firma de webhook inválida.")
        raise HTTPException(status_code=403, detail="Firma inválida")

    try:
        import json
        payload = json.loads(body_bytes)
    except Exception:
        return Response(content="ok", status_code=200)

    logger.info("Webhook recibido: type=%s", payload.get("type", "?"))

    background_tasks.add_task(_process_payload, payload)
    return Response(content="ok", status_code=200)


# ─── Procesamiento de mensajes ─────────────────────────────────────────────────

async def _process_payload(payload: dict) -> None:
    """Extrae y procesa el mensaje del payload de Meta."""
    msg = whatsapp.extract_message(payload)
    if not msg:
        return  # Status update u otro evento sin mensaje

    phone = msg["phone"]
    message_id = msg["message_id"]

    # Deduplicación
    if message_id in processed_message_ids:
        return
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > MAX_PROCESSED_IDS:
        # Limpiar la mitad más antigua
        old = list(processed_message_ids)[:MAX_PROCESSED_IDS // 2]
        for mid in old:
            processed_message_ids.discard(mid)

    # Marcar como leído
    await whatsapp.mark_as_read(message_id)

    msg_type = msg["type"]

    if msg_type == "text":
        text = msg["text"] or ""
        await _handle_text(phone, text)

    elif msg_type == "interactive":
        btn = msg["button_reply"] or {}
        btn_id = btn.get("id", "")
        btn_title = btn.get("title", "")
        await _handle_button(phone, btn_id, btn_title)

    elif msg_type == "document":
        doc = msg["document"] or {}
        if "pdf" in doc.get("mime_type", "").lower():
            await _handle_pdf(phone, doc)
        else:
            await whatsapp.send_text(
                phone,
                "Solo acepto PDFs de resúmenes de tarjeta de crédito. "
                "Enviá el archivo PDF directamente."
            )
    else:
        # Tipo no soportado (imagen, audio, sticker, etc.)
        await whatsapp.send_text(
            phone,
            "Solo proceso mensajes de texto y PDFs de resúmenes de tarjeta. "
            "¿En qué te puedo ayudar?"
        )


async def _handle_text(phone: str, text: str) -> None:
    """Procesa un mensaje de texto."""
    # Comandos especiales
    text_lower = text.strip().lower()
    if text_lower in ("/start", "inicio", "hola", "empezar", "start"):
        user = user_store.get_user(phone)
        if user and user.get("setup_complete"):
            conversations.pop(phone, None)
            await whatsapp.send_text(
                phone,
                f"¡Hola de nuevo, {user.get('nombre', '')}! 👋\n"
                "Historial reiniciado. ¿En qué te ayudo?"
            )
        else:
            await onboarding.start_onboarding(phone)
        return

    if text_lower in ("ayuda", "/help", "help", "?"):
        user = user_store.get_user(phone)
        if user and user.get("setup_complete"):
            await _send_help(phone)
        else:
            await onboarding.start_onboarding(phone)
        return

    if text_lower in ("/planilla", "planilla", "mi planilla", "link"):
        user = user_store.get_user(phone)
        if user and user.get("setup_complete"):
            url = user.get("sheet_url", "")
            await whatsapp.send_text(phone, f"📊 Tu planilla de Google Sheets:\n{url}")
        else:
            await whatsapp.send_text(phone, "Primero completá la configuración mandando *inicio*.")
        return

    if text_lower in ("/migrar", "migrar"):
        await _cmd_migrar(phone)
        return

    if text_lower in ("/clear", "limpiar", "clear", "borrar historial"):
        conversations.pop(phone, None)
        pending_pdfs.pop(phone, None)
        await whatsapp.send_text(phone, "Historial limpiado. Empezamos de cero.")
        return

    # Verificar si está en onboarding
    user = user_store.get_user(phone)
    if not user or not user.get("setup_complete"):
        consumed = await onboarding.handle_message(phone, text)
        if not consumed:
            await onboarding.start_onboarding(phone)
        return

    # Flujo normal con Claude
    sheets.set_active_sheet(user["sheet_id"])

    if phone not in conversations:
        conversations[phone] = []
    conversations[phone].append({"role": "user", "content": text})

    if len(conversations[phone]) > MAX_HISTORY:
        conversations[phone] = conversations[phone][-MAX_HISTORY:]

    try:
        response = _agent_loop(phone, user)
        await whatsapp.send_text(phone, response)
    except Exception as e:
        logger.error("handle_text error phone=%s: %s", phone, e, exc_info=True)
        if "overloaded" in str(e).lower():
            await whatsapp.send_text(
                phone,
                "Los servidores de Claude están saturados en este momento. "
                "Si registraste un gasto, revisá la planilla por las dudas. "
                "En unos minutos debería volver a funcionar."
            )
        else:
            await whatsapp.send_text(phone, "Ocurrió un error interno. Intentá de nuevo.")


async def _handle_button(phone: str, btn_id: str, btn_title: str) -> None:
    """Procesa respuestas de botones interactivos."""
    user = user_store.get_user(phone)
    if not user or not user.get("setup_complete"):
        consumed = await onboarding.handle_button_reply(phone, btn_id, btn_title)
        if not consumed:
            await onboarding.start_onboarding(phone)
        return

    # Botones fuera del onboarding: tratar como texto
    await _handle_text(phone, btn_title)


async def _handle_pdf(phone: str, doc: dict) -> None:
    """Descarga un PDF y lo procesa con el agente."""
    user = user_store.get_user(phone)
    if not user or not user.get("setup_complete"):
        await whatsapp.send_text(phone, "Primero completá la configuración mandando *inicio*.")
        return

    await whatsapp.send_text(phone, "⏳ Descargando el PDF...")

    media_id = doc.get("media_id")
    filename = doc.get("filename", "resumen.pdf")

    content = await whatsapp.download_media(media_id)
    if not content:
        await whatsapp.send_text(
            phone,
            "No pude descargar el PDF. Intentá enviarlo de nuevo."
        )
        return

    # Guardar en tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(content)
    tmp.close()
    pending_pdfs[phone] = tmp.name

    sheets.set_active_sheet(user["sheet_id"])

    if phone not in conversations:
        conversations[phone] = []
    conversations[phone].append({
        "role": "user",
        "content": (
            f"Te envié un PDF de resumen de tarjeta de crédito ({filename}). "
            "Necesito que lo proceses para importar las transacciones."
        ),
    })

    try:
        response = _agent_loop(phone, user)
        await whatsapp.send_text(phone, response)
    except Exception as e:
        logger.error("handle_pdf error phone=%s: %s", phone, e, exc_info=True)
        await whatsapp.send_text(phone, "Ocurrió un error al procesar el PDF. Intentá de nuevo.")


# ─── Agent loop (sync, llamado desde async con run_in_executor si hace falta) ──

def _agent_loop(phone: str, user: dict) -> str:
    """Loop de agente: Claude decide qué tools usar hasta dar una respuesta final."""
    system = _get_system_prompt(user)
    pdf_path = pending_pdfs.get(phone)

    for _ in range(10):
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=conversations[phone],
            tools=TOOL_DEFINITIONS,
        )

        conversations[phone].append({
            "role": "assistant",
            "content": [block.model_dump() for block in response.content],
        })

        if response.stop_reason == "end_turn":
            text_parts = [block.text for block in response.content if block.type == "text"]
            return "\n".join(text_parts) or "Listo."

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(
                        block.name, block.input,
                        user_config=user,
                        pdf_path=pdf_path,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                    if block.name == "parse_credit_card_pdf" and pdf_path:
                        pending_pdfs.pop(phone, None)
                        try:
                            os.unlink(pdf_path)
                        except OSError:
                            pass
                        pdf_path = None

            conversations[phone].append({"role": "user", "content": tool_results})
        else:
            return "Respuesta inesperada del modelo."

    return "Se alcanzó el límite de iteraciones. Intentá de nuevo."


# ─── Comandos especiales ────────────────────────────────────────────────────────

async def _send_help(phone: str) -> None:
    await whatsapp.send_text(
        phone,
        "*Axon Finance* — Asistente financiero personal 💰\n\n"
        "*¿Qué puedo hacer?*\n"
        "• Registrar gastos (efectivo, débito, tarjeta, cuotas)\n"
        "• Registrar ingresos y sueldos\n"
        "• Importar resúmenes de tarjeta en PDF (Santander, BBVA, Galicia, ICBC)\n"
        "• Consultar gastos por mes o categoría\n"
        "• Resumen mensual: gastos, ingresos, saldo, deuda de tarjetas\n"
        "• Estado de gastos fijos (alquiler, luz, internet, etc.)\n"
        "• Proyección del mes siguiente\n"
        "• Comparar dos meses\n"
        "• Registrar préstamos y devoluciones\n"
        "• Cotización del dólar blue\n\n"
        "*Comandos*\n"
        "inicio — Reiniciar el bot\n"
        "migrar — Archivar gastos del mes anterior\n"
        "planilla — Ver el link de tu Google Sheet\n"
        "limpiar — Borrar historial de conversación\n"
        "ayuda — Esta ayuda\n\n"
        "*Ejemplos*\n"
        '"Gasté $5.000 en el super"\n'
        '"Pagué el alquiler $200.000"\n'
        '"Cobré $500.000 de sueldo"\n'
        '"¿Cuánto gasté este mes?"'
    )


async def _cmd_migrar(phone: str) -> None:
    user = user_store.get_user(phone)
    if not user or not user.get("setup_complete"):
        await whatsapp.send_text(phone, "Primero completá la configuración mandando *inicio*.")
        return

    sheets.set_active_sheet(user["sheet_id"])
    await whatsapp.send_text(phone, "⏳ Migrando gastos al Histórico...")

    try:
        personas = user.get("personas", [user.get("nombre", "Usuario")])
        r = tools_archive.maybe_archive_past_months(carryover_persona=personas[0])

        if r["archivados"]:
            meses_str = ", ".join(MESES_ES.get(int(m[5:7]), m) for m in r["meses"])
            carryover = r["carryover"]
            if carryover["tipo"] == "deuda":
                co_msg = f"Saldo negativo: se cargó 'Deuda mes pasado' de ${carryover['monto']:,.0f} en Gastos."
            elif carryover["tipo"] == "saldo":
                co_msg = f"Saldo positivo: se cargó 'Saldo acumulado' de ${carryover['monto']:,.0f} en Ingresos."
            else:
                co_msg = "Saldo exactamente en cero."

            await whatsapp.send_text(
                phone,
                f"✅ Migración completada.\n"
                f"Meses archivados: {meses_str}\n"
                f"Gastos movidos al Histórico: {r['archivados']}\n"
                f"{co_msg}"
            )
        else:
            await whatsapp.send_text(
                phone,
                "No hay gastos de meses anteriores para migrar. La planilla ya está al día."
            )
    except Exception as e:
        logger.error("migrar error phone=%s: %s", phone, e, exc_info=True)
        await whatsapp.send_text(phone, "Ocurrió un error al migrar. Intentá de nuevo.")


# ─── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Axon Finance"}


# ─── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Limpiar PDFs temporales huérfanos
    tmp_dir = tempfile.gettempdir()
    for path in glob.glob(os.path.join(tmp_dir, "tmp*.pdf")):
        try:
            os.unlink(path)
        except OSError:
            pass
    logger.info("✅ Axon Finance Bot iniciado.")
