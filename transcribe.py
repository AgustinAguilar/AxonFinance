"""
transcribe.py — Transcripción de audio con Gemini.

Usa la API HTTP directa de Gemini (sin SDK pesado) para transcribir
mensajes de voz de WhatsApp (audio/ogg, opus).
"""

import base64
import logging

import httpx

from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT = (
    "Transcribí este audio al texto exacto, en español. "
    "Devolvé SOLO la transcripción, sin comillas ni aclaraciones."
)


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """Transcribe un audio con Gemini. Retorna el texto o None si falla."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY no configurado.")
        return None
    if not audio_bytes:
        return None

    # Normalizar mime type (WhatsApp suele mandar "audio/ogg; codecs=opus")
    mime = (mime_type or "audio/ogg").split(";")[0].strip()

    url = _ENDPOINT.format(model=GEMINI_MODEL)
    payload = {
        "contents": [{
            "parts": [
                {"text": _PROMPT},
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                    }
                },
            ]
        }],
        "generationConfig": {"temperature": 0.0},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(
                url,
                params={"key": GEMINI_API_KEY},
                json=payload,
            )
            if r.status_code != 200:
                logger.error("transcribe_audio → %s: %s", r.status_code, r.text[:400])
                return None
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                logger.error("transcribe_audio sin candidatos: %s", str(data)[:300])
                return None
            parts = candidates[0].get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or None
        except Exception as e:
            logger.error("transcribe_audio exception: %s", e)
            return None
