"""
tools_fixed.py — Gestión de gastos fijos (catálogo + registro de pagos).

origen = "manual"   → se paga por el bot (alquiler, luz, etc.)
origen = "tarjeta"  → viene del resumen PDF; NO se registra manualmente (evita duplicados).
"""

from datetime import datetime

import sheets
from sheets import safe_float
from config import TAB_GASTOS, TAB_GASTOS_FIJOS
import tools_finance


def _normalize(s: str) -> str:
    return s.strip().lower()


def _find_expense(nombre: str) -> dict | None:
    rows = sheets.get_all_rows(TAB_GASTOS_FIJOS)
    needle = _normalize(nombre)
    for row in rows:
        if str(row.get("activo", "")).upper() != "TRUE":
            continue
        if needle in _normalize(str(row.get("nombre", ""))):
            return row
    return None


def pay_fixed_expense(nombre: str, monto: float | None, persona: str) -> dict:
    expense = _find_expense(nombre)
    if not expense:
        rows = sheets.get_all_rows(TAB_GASTOS_FIJOS)
        activos = [r["nombre"] for r in rows if str(r.get("activo", "")).upper() == "TRUE"]
        return {
            "ok": False,
            "error": f"No encontré '{nombre}' en el catálogo de gastos fijos.",
            "gastos_fijos_disponibles": activos,
        }

    if str(expense.get("origen", "manual")).lower() == "tarjeta":
        return {
            "ok": False,
            "error": (
                f"'{expense['nombre']}' se paga con tarjeta y aparece automáticamente "
                f"en el resumen de la {expense.get('tarjeta', 'tarjeta')}. "
                "No hace falta registrarlo manualmente."
            ),
        }

    monto_a_usar = float(monto) if monto is not None else float(expense.get("monto_estimado", 0))
    if monto_a_usar <= 0:
        return {"ok": False, "error": "El monto no puede ser 0. Indicá un monto o actualizá el catálogo."}

    monto_anterior = float(expense.get("monto_estimado", 0))
    if monto is not None and abs(monto - monto_anterior) > 0.01:
        _update_monto_estimado(expense["nombre"], monto)

    result = tools_finance.add_expense(
        descripcion=expense["nombre"],
        monto=monto_a_usar,
        metodo_pago=expense.get("metodo_pago", "efectivo"),
        persona=persona,
        tarjeta=expense.get("tarjeta") or None,
        fecha=datetime.now().strftime("%Y-%m-%d"),
    )

    msg = f"Pagado: {expense['nombre']} ${monto_a_usar:,.0f}"
    if monto is not None and abs(monto - monto_anterior) > 0.01:
        msg += f" (precio actualizado de ${monto_anterior:,.0f} a ${monto:,.0f})"
    result["mensaje"] = msg
    return result


def get_fixed_expenses_status(mes: str | None = None) -> dict:
    mes = mes or datetime.now().strftime("%Y-%m")
    catalog = sheets.get_all_rows(TAB_GASTOS_FIJOS)
    activos = [r for r in catalog if str(r.get("activo", "")).upper() == "TRUE"]
    gastos_mes = sheets.get_gastos_for_month(mes)

    resumen = []
    total_estimado = 0.0
    total_pagado = 0.0

    for item in activos:
        nombre = item["nombre"]
        monto_estimado = safe_float(item.get("monto_estimado"))
        origen = str(item.get("origen", "manual")).lower()
        total_estimado += monto_estimado

        if origen == "tarjeta":
            match_key = _normalize(str(item.get("descripcion_tarjeta") or nombre))
            coincidencias = [
                g for g in gastos_mes
                if match_key in _normalize(str(g.get("descripcion", "")))
                and g.get("metodo_pago") == "tarjeta_credito"
            ]
            if coincidencias:
                monto_real = sum(safe_float(g.get("monto")) for g in coincidencias)
                total_pagado += monto_real
                resumen.append({
                    "nombre": nombre,
                    "estado": "pagado (tarjeta)",
                    "monto_estimado": monto_estimado,
                    "monto_real": monto_real,
                    "tarjeta": item.get("tarjeta", ""),
                })
            else:
                resumen.append({
                    "nombre": nombre,
                    "estado": "pendiente (falta importar resumen de tarjeta)",
                    "monto_estimado": monto_estimado,
                    "tarjeta": item.get("tarjeta", ""),
                })
        else:
            coincidencias = [
                g for g in gastos_mes
                if _normalize(nombre) in _normalize(str(g.get("descripcion", "")))
                and g.get("metodo_pago") != "tarjeta_credito"
            ]
            if coincidencias:
                monto_real = sum(safe_float(g.get("monto")) for g in coincidencias)
                total_pagado += monto_real
                resumen.append({
                    "nombre": nombre,
                    "estado": "pagado",
                    "monto_estimado": monto_estimado,
                    "monto_real": monto_real,
                    "metodo_pago": item.get("metodo_pago", ""),
                })
            else:
                resumen.append({
                    "nombre": nombre,
                    "estado": "pendiente",
                    "monto_estimado": monto_estimado,
                    "metodo_pago": item.get("metodo_pago", ""),
                })

    pendientes = [r for r in resumen if r["estado"].startswith("pendiente")]
    pagados = [r for r in resumen if r["estado"].startswith("pagado")]

    return {
        "ok": True,
        "mes": mes,
        "total_estimado": total_estimado,
        "total_pagado": total_pagado,
        "total_pendiente": total_estimado - total_pagado,
        "pagados": pagados,
        "pendientes": pendientes,
    }


def add_fixed_expense(nombre: str, monto_estimado: float, metodo_pago: str,
                      persona: str = "", tarjeta: str | None = None,
                      origen: str = "manual") -> dict:
    existing = _find_expense(nombre)
    if existing:
        return {
            "ok": False,
            "error": f"Ya existe '{existing['nombre']}' en el catálogo. Usá pay_fixed_expense para actualizar el monto.",
        }

    sheets.append_row(TAB_GASTOS_FIJOS, [
        nombre, monto_estimado, metodo_pago,
        tarjeta or "", origen, "TRUE", "", persona,
    ])
    return {
        "ok": True,
        "mensaje": f"'{nombre}' agregado al catálogo de gastos fijos (${monto_estimado:,.0f}/mes).",
    }


def _update_monto_estimado(nombre: str, nuevo_monto: float) -> None:
    ws = sheets.get_worksheet(TAB_GASTOS_FIJOS)
    all_values = ws.get_all_values()
    if not all_values:
        return
    headers = all_values[0]
    try:
        nombre_col = headers.index("nombre")
    except ValueError:
        return
    needle = _normalize(nombre)
    for i, row in enumerate(all_values[1:], start=2):
        if len(row) > nombre_col and needle in _normalize(row[nombre_col]):
            sheets.update_cell(TAB_GASTOS_FIJOS, i, "monto_estimado", nuevo_monto)
            return
