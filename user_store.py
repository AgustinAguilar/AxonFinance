"""
user_store.py — Persistencia de configuración de usuarios en users.json.

Cada entrada: chat_id (str) → dict con campos del usuario.
Campos principales:
  nombre, email, personas (list), tarjetas (list), ingreso_estimado,
  sheet_id, sheet_url, setup_complete, onboarding_state, onboarding_data
"""

import json
import os

STORE_FILE = os.path.join(os.path.dirname(__file__), "users.json")


def _load() -> dict:
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(chat_id: int) -> dict | None:
    return _load().get(str(chat_id))


def save_user(chat_id: int, user: dict) -> None:
    data = _load()
    data[str(chat_id)] = user
    _save(data)


def update_user(chat_id: int, **kwargs) -> dict:
    data = _load()
    key = str(chat_id)
    if key not in data:
        data[key] = {}
    data[key].update(kwargs)
    _save(data)
    return data[key]


def is_setup_complete(chat_id: int) -> bool:
    user = get_user(chat_id)
    return bool(user and user.get("setup_complete"))


def get_all_users() -> dict:
    return _load()
