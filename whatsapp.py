"""
whatsapp.py — Cliente de YCloud WhatsApp API.

Responsabilidad única: toda la comunicación HTTP con YCloud.
No contiene lógica de negocio.
"""

import hmac
import hashlib
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_API_KEY = lambda: os.getenv("YCLOUD_API_KEY", "")
_FROM = lambda: os.getenv("YCLOUD_PHONE_NUMBER", "")
_WEBHOOK_SECRET = lambda: os.getenv("YCLOUD_WEBHOOK_SECRET", "")
BASE_URL = "https://api.ycloud.com/v2/whatsapp"


def _headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": _API_KEY(),
    }


# ─── Envío de mensajes ─────────────────────────────────────────────────────────

async def send_text(phone: str, text: str) -> None:
    """Envía uno o más mensajes de texto (chunking automático a 4000 chars)."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    to = _normalize_to(phone)
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            try:
                r = await client.post(
                    f"{BASE_URL}/messages/sendDirectly",
                    headers=_headers(),
                    json={
                        "from": _FROM(),
                        "to": to,
                        "type": "text",
                        "text": {"body": chunk, "previewUrl": False},
                    },
                )
                if r.status_code not in (200, 201):
                    logger.error("send_text %s → %s: %s", to, r.status_code, r.text[:300])
            except Exception as e:
                logger.error("send_text exception: %s", e)


async def send_buttons(phone: str, body: str, buttons: list[dict]) -> None:
    """
    Envía un mensaje con botones de respuesta rápida (max 3).
    buttons: [{"id": "btn_id", "title": "Texto (max 20 chars)"}, ...]
    """
    to = _normalize_to(phone)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/messages/sendDirectly",
                headers=_headers(),
                json={
                    "from": _FROM(),
                    "to": to,
                    "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": body},
                        "action": {
                            "buttons": [
                                {"type": "reply", "reply": btn}
                                for btn in buttons[:3]
                            ]
                        },
                    },
                },
            )
            if r.status_code not in (200, 201):
                logger.error("send_buttons %s → %s: %s", to, r.status_code, r.text[:300])
        except Exception as e:
            logger.error("send_buttons exception: %s", e)


async def mark_as_read(message_id: str) -> None:
    """Marca el mensaje entrante como leído (tick azul)."""
    if not message_id:
        return
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.post(
                f"{BASE_URL}/inboundMessages/{message_id}/markAsRead",
                headers=_headers(),
            )
        except Exception:
            pass


# ─── Descarga de media (PDFs) ──────────────────────────────────────────────────

async def get_media_url(media_id: str) -> str | None:
    """Obtiene la URL firmada de descarga de un media_id vía YCloud."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/media/{media_id}",
                headers=_headers(),
            )
            if r.status_code == 200:
                return r.json().get("url")
            logger.error("get_media_url %s → %s: %s", media_id, r.status_code, r.text[:200])
        except Exception as e:
            logger.error("get_media_url exception: %s", e)
    return None


async def download_media(media_id: str) -> bytes | None:
    """Descarga el contenido binario de un media (PDF, imagen, etc.)."""
    url = await get_media_url(media_id)
    if not url:
        return None
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={"X-API-Key": _API_KEY()})
            if r.status_code == 200:
                return r.content
            logger.error("download_media %s → %s", media_id, r.status_code)
        except Exception as e:
            logger.error("download_media exception: %s", e)
    return None


# ─── Seguridad del webhook ─────────────────────────────────────────────────────

def verify_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verifica la firma de YCloud. Header format:
    "YCloud-Signature: t={timestamp},s={signature}"
    Firma HMAC-SHA256 de "{timestamp}.{body}." con el webhook secret.
    """
    secret = _WEBHOOK_SECRET()
    if not secret:
        logger.warning("YCLOUD_WEBHOOK_SECRET no configurado. Omitiendo verificación.")
        return True
    if not signature_header:
        logger.warning("Falta header YCloud-Signature.")
        return False
    try:
        parts = dict(p.strip().split("=", 1) for p in signature_header.split(","))
        timestamp = parts.get("t", "")
        signature = parts.get("s", "")
        body = payload_bytes.decode("utf-8")

        # Probar ambas variantes del secret (con y sin prefijo whsec_)
        secrets_to_try = [secret]
        if secret.startswith("whsec_"):
            secrets_to_try.append(secret[len("whsec_"):])

        for sec in secrets_to_try:
            for signed in (f"{timestamp}.{body}.", f"{timestamp}.{body}"):
                expected = hmac.new(sec.encode(), signed.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(expected, signature):
                    return True

        # Log de diagnóstico (primeras 12 chars para no leakear)
        computed = hmac.new(
            secret.encode(),
            f"{timestamp}.{body}.".encode(),
            hashlib.sha256,
        ).hexdigest()
        logger.warning(
            "Firma no coincide. ts=%s recv=%s... calc=%s... body_len=%d",
            timestamp, signature[:12], computed[:12], len(body),
        )
        return False
    except Exception as e:
        logger.error("verify_signature error: %s", e)
        return False


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normalize_to(phone: str) -> str:
    """Asegura formato E.164 con '+' al inicio."""
    phone = (phone or "").strip()
    if not phone:
        return phone
    return phone if phone.startswith("+") else f"+{phone}"


def extract_message(payload: dict) -> dict | None:
    """
    Extrae info relevante del webhook de YCloud.
    Retorna None si no es un inbound message.

    Formato esperado (basado en eventos de YCloud):
    {
      "type": "whatsapp.inbound_message.received",
      "whatsappInboundMessage": {
        "id": "wamid...",
        "wabaId": "...",
        "from": "5491158171784",
        "to": "5491157501453",
        "type": "text" | "interactive" | "document" | ...,
        "text": {"body": "..."},
        "interactive": {...},
        "document": {"id": "...", "filename": "...", "mimeType": "..."},
        "profileName": "..."
      }
    }
    """
    try:
        event_type = payload.get("type") or payload.get("event_type") or ""
        if event_type and "inbound_message" not in event_type:
            return None

        inbound = (
            payload.get("whatsappInboundMessage")
            or payload.get("inboundMessage")
            or payload
        )
        if not isinstance(inbound, dict):
            return None

        msg_type = inbound.get("type")
        if not msg_type:
            return None

        phone = inbound.get("from") or inbound.get("waId") or ""
        if phone.startswith("+"):
            phone = phone[1:]

        result = {
            "phone": phone,
            "message_id": inbound.get("id") or inbound.get("wamid") or "",
            "type": msg_type,
            "name": inbound.get("profileName") or inbound.get("customerProfileName", ""),
            "text": None,
            "button_reply": None,
            "document": None,
            "audio": None,
        }

        if msg_type == "text":
            text_obj = inbound.get("text") or {}
            result["text"] = text_obj.get("body", "") if isinstance(text_obj, dict) else str(text_obj)

        elif msg_type == "interactive":
            interactive = inbound.get("interactive", {})
            itype = interactive.get("type")
            if itype == "button_reply":
                result["button_reply"] = interactive.get("button_reply", {})
            elif itype == "list_reply":
                result["button_reply"] = interactive.get("list_reply", {})

        elif msg_type == "document":
            doc = inbound.get("document", {})
            result["document"] = {
                "media_id": doc.get("id") or doc.get("mediaId"),
                "filename": doc.get("filename", "documento.pdf"),
                "mime_type": doc.get("mimeType") or doc.get("mime_type", ""),
            }

        elif msg_type in ("audio", "voice"):
            audio = inbound.get("audio") or inbound.get("voice") or {}
            result["audio"] = {
                "media_id": audio.get("id") or audio.get("mediaId"),
                "mime_type": audio.get("mimeType") or audio.get("mime_type", "audio/ogg"),
            }

        return result
    except Exception as e:
        logger.error("extract_message error: %s — payload=%s", e, str(payload)[:500])
        return None
