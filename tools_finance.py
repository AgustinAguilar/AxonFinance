import uuid
from datetime import datetime

from config import TAB_GASTOS, TAB_INGRESOS, TAB_TARJETAS, TAB_AHORROS
import sheets
from sheets import safe_float


def add_expense(descripcion: str, monto: float, metodo_pago: str,
                persona: str, tarjeta: str | None = None,
                cuotas: int | None = None, fecha: str | None = None,
                categoria: str | None = None) -> dict:
    gasto_id = uuid.uuid4().hex[:8]
    fecha = fecha or datetime.now().strftime("%Y-%m-%d")
    cuotas = max(1, cuotas) if cuotas else 1

    monto_cuota = round(monto / cuotas, 2) if cuotas > 1 else ""
    cuotas_total = cuotas if cuotas > 1 else ""
    cuota_actual = 1 if cuotas > 1 else ""

    row = [
        gasto_id, fecha, persona, descripcion, monto,
        metodo_pago, tarjeta or "", cuotas_total, cuota_actual,
        monto_cuota, "manual", categoria or "",
    ]
    sheets.append_row(TAB_GASTOS, row)

    if cuotas > 1:
        sheets.append_row(TAB_TARJETAS, [
            descripcion, tarjeta or "", persona,
            1, cuotas, round(monto / cuotas, 2),
        ])
        return {
            "ok": True,
            "mensaje": (
                f"Gasto registrado: {descripcion} ${monto:,.0f} "
                f"en {cuotas} cuotas de ${monto/cuotas:,.0f} con {tarjeta}"
            ),
            "id": gasto_id,
        }

    return {
        "ok": True,
        "mensaje": f"Gasto registrado: {descripcion} ${monto:,.0f} ({metodo_pago})",
        "id": gasto_id,
    }


def add_income(monto: float, tipo: str, persona: str,
               descripcion: str | None = None, fecha: str | None = None) -> dict:
    income_id = uuid.uuid4().hex[:8]
    fecha = fecha or datetime.now().strftime("%Y-%m-%d")
    descripcion = descripcion or ("Sueldo" if tipo == "salario" else "Ingreso")

    row = [income_id, fecha, persona, tipo, monto, descripcion]
    sheets.append_row(TAB_INGRESOS, row)

    return {
        "ok": True,
        "mensaje": f"Ingreso registrado: ${monto:,.0f} ({tipo}) — {descripcion}",
        "id": income_id,
    }


def get_expenses(persona: str | None = None, mes: str | None = None,
                 metodo_pago: str | None = None, limit: int = 20) -> dict:
    if mes:
        rows = sheets.get_gastos_for_month(mes)
    else:
        rows = sheets.get_all_rows(TAB_GASTOS)

    if persona:
        rows = [r for r in rows if r.get("persona") == persona]
    if metodo_pago:
        rows = [r for r in rows if r.get("metodo_pago") == metodo_pago]

    if not rows:
        return {"ok": True, "gastos": [], "mensaje": "No se encontraron gastos con esos filtros."}

    total = sum(safe_float(r.get("monto")) for r in rows)
    cantidad_total = len(rows)

    if not mes:
        rows = rows[-limit:]

    return {
        "ok": True,
        "gastos": rows,
        "total": total,
        "cantidad": cantidad_total,
    }


def delete_expense(gasto_id: str, persona: str) -> dict:
    if gasto_id == "ultimo":
        rows = sheets.get_all_rows(TAB_GASTOS)
        user_rows = [r for r in rows if r.get("persona") == persona]
        if not user_rows:
            return {"ok": False, "mensaje": "No hay gastos para eliminar."}
        gasto_id = str(user_rows[-1].get("id", ""))

    deleted = sheets.delete_row_by_id(TAB_GASTOS, "id", gasto_id)
    if not deleted:
        return {"ok": False, "mensaje": f"No se encontró gasto con id {gasto_id}."}

    return {"ok": True, "mensaje": f"Gasto {gasto_id} eliminado."}


def add_saving(monto: float, tipo: str, moneda: str = "ARS", persona: str = "",
               notas: str | None = None, fecha: str | None = None) -> dict:
    from tools_dashboard import get_dolar_blue

    fecha = fecha or datetime.now().strftime("%Y-%m-%d")
    moneda = moneda.upper()

    if moneda == "USD":
        dolar = get_dolar_blue()
        if not dolar.get("ok"):
            return {"ok": False, "error": "No se pudo obtener la cotización del dólar blue. Intentá de nuevo."}
        cotizacion = dolar["venta"]
        monto_ars = round(monto * cotizacion, 2)
        monto_usd = monto
    else:
        cotizacion = ""
        monto_ars = monto
        monto_usd = ""

    sheets.append_row(TAB_AHORROS, [
        fecha, persona, tipo, monto_ars, cotizacion, monto_usd, notas or "",
    ])

    gasto_id = uuid.uuid4().hex[:8]
    sheets.append_row(TAB_GASTOS, [
        gasto_id, fecha, persona, "Deposito Ahorro", monto_ars,
        "efectivo", "", "", "", "", "manual", "otros",
    ])

    if moneda == "USD":
        return {
            "ok": True,
            "mensaje": (
                f"Ahorro registrado ({tipo}): USD {monto:,.0f} = ARS ${monto_ars:,.0f} "
                f"(blue ${cotizacion:,.0f}). Se registró 'Deposito Ahorro' como gasto."
            ),
        }
    return {
        "ok": True,
        "mensaje": f"Ahorro registrado ({tipo}): ${monto_ars:,.0f} ARS. Se registró 'Deposito Ahorro' como gasto.",
    }
