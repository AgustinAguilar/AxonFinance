"""
user_store.py — Persistencia de configuración de usuarios en users.json.

Cada entrada: chat_id (str) → dict con campos del usuario.
Campos principales:
  nombre, email, personas (list), tarjetas (list), ingreso_estimado,
  sheet_id, sheet_url, setup_complete, onboarding_state, onboarding_data

Campos de uso (tracking):
  usage: {
    tokens_input_total, tokens_output_total, cost_usd_total, messages_total,
    tokens_input_month, tokens_output_month, cost_usd_month, messages_month,
    last_month_key (YYYY-MM)
  }
"""

import json
import os
from datetime import datetime

STORE_FILE = os.path.join(os.path.dirname(__file__), "users.json")

# Pricing Claude Sonnet 4.6 (USD por 1M tokens) — actualizar si cambia
PRICE_INPUT_PER_MTOK = 3.0
PRICE_OUTPUT_PER_MTOK = 15.0


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


def add_usage(chat_id: int, input_tokens: int, output_tokens: int) -> None:
    """Acumula uso de tokens y costo. Resetea contadores mensuales si cambió el mes."""
    data = _load()
    key = str(chat_id)
    if key not in data:
        return

    usage = data[key].get("usage") or {}
    now_month = datetime.now().strftime("%Y-%m")
    if usage.get("last_month_key") != now_month:
        usage["tokens_input_month"] = 0
        usage["tokens_output_month"] = 0
        usage["cost_usd_month"] = 0.0
        usage["messages_month"] = 0
        usage["last_month_key"] = now_month

    cost = (input_tokens * PRICE_INPUT_PER_MTOK + output_tokens * PRICE_OUTPUT_PER_MTOK) / 1_000_000

    usage["tokens_input_total"] = usage.get("tokens_input_total", 0) + input_tokens
    usage["tokens_output_total"] = usage.get("tokens_output_total", 0) + output_tokens
    usage["cost_usd_total"] = round(usage.get("cost_usd_total", 0.0) + cost, 6)
    usage["messages_total"] = usage.get("messages_total", 0) + 1
    usage["tokens_input_month"] += input_tokens
    usage["tokens_output_month"] += output_tokens
    usage["cost_usd_month"] = round(usage["cost_usd_month"] + cost, 6)
    usage["messages_month"] += 1

    data[key]["usage"] = usage
    _save(data)


def get_usage_summary() -> dict:
    """Resumen agregado de todos los usuarios para endpoint admin."""
    data = _load()
    totals = {
        "users": len(data),
        "tokens_input_total": 0,
        "tokens_output_total": 0,
        "cost_usd_total": 0.0,
        "messages_total": 0,
        "tokens_input_month": 0,
        "tokens_output_month": 0,
        "cost_usd_month": 0.0,
        "messages_month": 0,
    }
    per_user = []
    now_month = datetime.now().strftime("%Y-%m")
    for phone, u in data.items():
        usage = u.get("usage") or {}
        month_active = usage.get("last_month_key") == now_month
        totals["tokens_input_total"] += usage.get("tokens_input_total", 0)
        totals["tokens_output_total"] += usage.get("tokens_output_total", 0)
        totals["cost_usd_total"] += usage.get("cost_usd_total", 0.0)
        totals["messages_total"] += usage.get("messages_total", 0)
        if month_active:
            totals["tokens_input_month"] += usage.get("tokens_input_month", 0)
            totals["tokens_output_month"] += usage.get("tokens_output_month", 0)
            totals["cost_usd_month"] += usage.get("cost_usd_month", 0.0)
            totals["messages_month"] += usage.get("messages_month", 0)
        per_user.append({
            "phone": phone,
            "nombre": u.get("nombre", ""),
            "messages_total": usage.get("messages_total", 0),
            "messages_month": usage.get("messages_month", 0) if month_active else 0,
            "cost_usd_total": round(usage.get("cost_usd_total", 0.0), 4),
            "cost_usd_month": round(usage.get("cost_usd_month", 0.0), 4) if month_active else 0,
        })
    totals["cost_usd_total"] = round(totals["cost_usd_total"], 4)
    totals["cost_usd_month"] = round(totals["cost_usd_month"], 4)
    per_user.sort(key=lambda x: x["cost_usd_month"], reverse=True)
    return {"totals": totals, "users": per_user}
