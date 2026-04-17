"""
oauth.py — Flujo OAuth 2.0 con Google para acceso al Drive del cliente.

Scope: https://www.googleapis.com/auth/drive.file (no-sensitive, no requiere
verificación de Google). La app solo accede a archivos que ella misma crea.

Flujo:
  1. Bot manda link: GET /auth/{phone}
  2. Servidor redirige a Google OAuth con state firmado.
  3. Google llama GET /auth/callback?code=...&state=...
  4. Servidor intercambia code por tokens, los guarda en users.json,
     actualiza el onboarding_state a WAIT_NAME y manda siguiente pregunta.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import user_store
from config import (
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_OAUTH_SCOPES,
    STATE_SECRET,
)

logger = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_STATE_TTL_SECONDS = 600  # 10 min


def _sign_state(phone: str) -> str:
    """Genera un state firmado con HMAC: base64({phone, ts, nonce}).signature"""
    payload = {
        "phone": phone,
        "ts": int(time.time()),
        "nonce": secrets.token_urlsafe(8),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(STATE_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{b64}.{sig}"


def _verify_state(state: str) -> str | None:
    """Verifica un state firmado y retorna el phone si es válido."""
    if not state or "." not in state:
        return None
    try:
        b64, sig = state.rsplit(".", 1)
        expected_sig = hmac.new(STATE_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(expected_sig, sig):
            return None
        padding = "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64 + padding))
        if time.time() - payload["ts"] > _STATE_TTL_SECONDS:
            return None
        return payload["phone"]
    except Exception as e:
        logger.warning("verify_state error: %s", e)
        return None


def build_authorize_url(phone: str) -> str:
    """Genera la URL de Google OAuth para iniciar el flujo para un phone."""
    state = _sign_state(phone)
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",   # nos da refresh_token
        "prompt": "consent",        # fuerza mostrar consentimiento (asegura refresh_token)
        "state": state,
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict | None:
    """Intercambia el authorization code por tokens."""
    data = {
        "code": code,
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(_TOKEN_URL, data=data)
    if r.status_code != 200:
        logger.error("exchange_code %s: %s", r.status_code, r.text[:300])
        return None
    return r.json()


async def fetch_userinfo(access_token: str) -> dict | None:
    """Obtiene email del usuario usando el access_token."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code != 200:
        logger.warning("fetch_userinfo %s: %s", r.status_code, r.text[:200])
        return None
    return r.json()


def process_callback_sync(phone: str, tokens: dict, userinfo: dict | None) -> None:
    """Guarda tokens en users.json para el phone dado."""
    expires_in = tokens.get("expires_in", 3600)
    oauth_data = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(expires_in) - 60,  # 1 min de margen
        "scope": tokens.get("scope", ""),
        "connected_at": int(time.time()),
    }
    if userinfo:
        oauth_data["google_email"] = userinfo.get("email", "")

    user_store.update_user(phone, oauth=oauth_data)


def get_credentials_for_phone(phone: str) -> Credentials | None:
    """
    Retorna Credentials de OAuth para el phone, refrescando si hace falta.
    Guarda el nuevo access_token en users.json.
    """
    user = user_store.get_user(phone)
    if not user or "oauth" not in user:
        return None
    oauth = user["oauth"]
    refresh_token = oauth.get("refresh_token")
    if not refresh_token:
        return None

    creds = Credentials(
        token=oauth.get("access_token"),
        refresh_token=refresh_token,
        token_uri=_TOKEN_URL,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=list(GOOGLE_OAUTH_SCOPES),
    )

    # Refrescar si expiró
    if not creds.valid or int(time.time()) > oauth.get("expires_at", 0):
        try:
            creds.refresh(Request())
            # Persistir el nuevo token
            oauth["access_token"] = creds.token
            oauth["expires_at"] = int(time.time()) + 3600 - 60
            user_store.update_user(phone, oauth=oauth)
        except Exception as e:
            logger.error("refresh token failed phone=%s: %s", phone, e)
            return None

    return creds


def revoke(phone: str) -> None:
    """Borra los tokens del usuario (si quiere desconectarse)."""
    user = user_store.get_user(phone) or {}
    if "oauth" in user:
        user.pop("oauth", None)
        user_store.save_user(phone, user)
