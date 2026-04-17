import uuid
from datetime import datetime

import sheets
from config import TAB_PRESTAMOS


def add_loan(contraparte: str, monto: float, moneda: str, tipo: str,
             persona: str, fecha: str | None = None, notas: str | None = None) -> dict:
    """
    Registra un evento de préstamo o devolución.
    tipo: prestamo_dado | prestamo_recibido | devolucion_recibida | devolucion_dada
    """
    loan_id = uuid.uuid4().hex[:8]
    fecha = fecha or datetime.now().strftime("%Y-%m-%d")
    moneda = moneda.upper()

    sheets.append_row(TAB_PRESTAMOS, [
        loan_id, fecha, persona, contraparte, tipo, monto, moneda, notas or "",
    ])

    tipo_labels = {
        "prestamo_dado":       f"Le prestaste ${monto:,.0f} {moneda} a {contraparte}",
        "prestamo_recibido":   f"{contraparte} te prestó ${monto:,.0f} {moneda}",
        "devolucion_recibida": f"{contraparte} te devolvió ${monto:,.0f} {moneda}",
        "devolucion_dada":     f"Le devolviste ${monto:,.0f} {moneda} a {contraparte}",
    }

    return {
        "ok": True,
        "mensaje": tipo_labels.get(tipo, f"Registrado: {tipo} ${monto:,.0f} {moneda} — {contraparte}"),
        "id": loan_id,
    }


def get_loans_balance(contraparte: str | None = None) -> dict:
    """Calcula el balance neto de préstamos por persona, separado por moneda."""
    rows = sheets.get_all_rows(TAB_PRESTAMOS)
    balances: dict[str, dict] = {}

    for r in rows:
        cp = str(r.get("contraparte", "")).strip()
        if not cp:
            continue
        if contraparte and cp.lower() != contraparte.lower():
            continue

        tipo   = str(r.get("tipo", ""))
        moneda = str(r.get("moneda", "ARS")).upper()
        try:
            monto = float(r.get("monto", 0))
        except (ValueError, TypeError):
            continue

        if cp not in balances:
            balances[cp] = {
                "me_deben_ars": 0.0, "me_deben_usd": 0.0,
                "les_debo_ars": 0.0, "les_debo_usd": 0.0,
            }

        m = moneda.lower()
        if tipo == "prestamo_dado":
            balances[cp][f"me_deben_{m}"] += monto
        elif tipo == "devolucion_recibida":
            balances[cp][f"me_deben_{m}"] -= monto
        elif tipo == "prestamo_recibido":
            balances[cp][f"les_debo_{m}"] += monto
        elif tipo == "devolucion_dada":
            balances[cp][f"les_debo_{m}"] -= monto

    result = [
        {
            "contraparte": cp,
            "me_deben_ars": round(max(0.0, b["me_deben_ars"]), 2),
            "me_deben_usd": round(max(0.0, b["me_deben_usd"]), 2),
            "les_debo_ars": round(max(0.0, b["les_debo_ars"]), 2),
            "les_debo_usd": round(max(0.0, b["les_debo_usd"]), 2),
        }
        for cp, b in balances.items()
    ]

    if not result and contraparte:
        return {"ok": False, "error": f"No hay préstamos registrados con {contraparte}."}

    return {"ok": True, "prestamos": result}
