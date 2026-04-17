import time
from datetime import datetime

import httpx

from config import TAB_GASTOS, TAB_INGRESOS, TAB_TARJETAS, TAB_AHORROS, TAB_GASTOS_FIJOS
import sheets
from sheets import safe_float

_dolar_cache: dict = {"ok": False, "ts": 0}
_DOLAR_TTL = 300  # 5 minutos


def get_dolar_blue() -> dict:
    global _dolar_cache
    if _dolar_cache.get("ok") and time.time() - _dolar_cache["ts"] < _DOLAR_TTL:
        return _dolar_cache

    last_error = ""
    for attempt in range(3):
        try:
            resp = httpx.get("https://dolarapi.com/v1/dolares/blue", timeout=10)
            data = resp.json()
            compra = data.get("compra")
            venta = data.get("venta")
            if not isinstance(compra, (int, float)) or not isinstance(venta, (int, float)) \
                    or venta <= 0 or compra <= 0:
                raise ValueError(f"Cotización inválida: compra={compra}, venta={venta}")
            _dolar_cache = {
                "ok": True,
                "compra": compra,
                "venta": venta,
                "fecha": data.get("fechaActualizacion", ""),
                "ts": time.time(),
            }
            return _dolar_cache
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return {"ok": False, "error": last_error}


def get_monthly_summary(persona: str | None = None, mes: str | None = None) -> dict:
    mes = mes or datetime.now().strftime("%Y-%m")

    gastos_mes = sheets.get_gastos_for_month(mes)
    if persona:
        gastos_mes = [g for g in gastos_mes if g.get("persona") == persona]
    total_gastos = sum(safe_float(g.get("monto")) for g in gastos_mes)

    ingresos = sheets.get_all_rows(TAB_INGRESOS)
    ingresos_mes = [i for i in ingresos if str(i.get("fecha", "")).startswith(mes)]
    if persona:
        ingresos_mes = [i for i in ingresos_mes if i.get("persona") == persona]
    total_ingresos = sum(safe_float(i.get("monto")) for i in ingresos_mes)

    # Cuotas pendientes
    tarjetas = sheets.get_all_rows(TAB_TARJETAS)
    cuotas_pendientes = tarjetas if not persona else [t for t in tarjetas if t.get("persona") == persona]
    deuda_tarjetas = sum(safe_float(t.get("monto_cuota")) for t in cuotas_pendientes)

    # Deuda por tarjeta
    deuda_por_tarjeta: dict[str, float] = {}
    for t in cuotas_pendientes:
        banco = str(t.get("tarjeta", "Desconocida"))
        deuda_por_tarjeta[banco] = deuda_por_tarjeta.get(banco, 0.0) + safe_float(t.get("monto_cuota"))

    saldo = total_ingresos - total_gastos

    dolar = get_dolar_blue()
    dolar_venta = dolar.get("venta", 0) if dolar["ok"] else 0

    return {
        "ok": True,
        "mes": mes,
        "persona": persona or "Todos",
        "total_gastos": total_gastos,
        "total_ingresos": total_ingresos,
        "deuda_tarjetas": deuda_tarjetas,
        "deuda_por_tarjeta": deuda_por_tarjeta,
        "saldo": saldo,
        "dolar_blue_venta": dolar_venta,
    }


def get_credit_card_debt(tarjeta: str | None = None, persona: str | None = None) -> dict:
    pendientes = sheets.get_all_rows(TAB_TARJETAS)

    if tarjeta:
        pendientes = [t for t in pendientes if t.get("tarjeta") == tarjeta]
    if persona:
        pendientes = [t for t in pendientes if t.get("persona") == persona]

    total = sum(safe_float(t.get("monto_cuota")) for t in pendientes)

    return {
        "ok": True,
        "cuotas_pendientes": len(pendientes),
        "total_deuda": total,
        "detalle": pendientes[:30],
    }


def get_next_month_projection() -> dict:
    now = datetime.now()
    next_month = now.month + 1
    next_year = now.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    mes_proyectado = f"{next_year}-{next_month:02d}"

    gastos_fijos_rows = sheets.get_all_rows(TAB_GASTOS_FIJOS)
    fijos_activos = [r for r in gastos_fijos_rows if str(r.get("activo", "")).upper() == "TRUE"]
    total_fijos = sum(safe_float(r.get("monto_estimado")) for r in fijos_activos)
    detalle_fijos = [
        {"nombre": r.get("nombre", ""), "monto": safe_float(r.get("monto_estimado"))}
        for r in fijos_activos
    ]

    tarjetas = sheets.get_all_rows(TAB_TARJETAS)
    total_cuotas = sum(safe_float(t.get("monto_cuota")) for t in tarjetas)

    nombres_fijos = {str(r.get("nombre", "")).lower() for r in gastos_fijos_rows}
    match_descs = {
        str(r.get("descripcion_tarjeta", "")).lower().strip()
        for r in gastos_fijos_rows
        if str(r.get("descripcion_tarjeta", "")).strip()
    }
    exclusiones = nombres_fijos | match_descs

    meses_pasados = []
    for delta in range(1, 4):
        m = now.month - delta
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        meses_pasados.append(f"{y}-{m:02d}")

    totales_variables = []
    for mes in meses_pasados:
        gastos_mes = [
            g for g in sheets.get_gastos_for_month(mes)
            if g.get("metodo_pago") != "tarjeta_credito"
            and str(g.get("descripcion", "")).lower() not in exclusiones
        ]
        totales_variables.append(sum(safe_float(g.get("monto")) for g in gastos_mes))

    gastos_variables_estimados = (
        round(sum(totales_variables) / len(totales_variables), 2) if totales_variables else 0
    )
    total_proyectado = round(total_fijos + total_cuotas + gastos_variables_estimados, 2)

    return {
        "ok": True,
        "mes_proyectado": mes_proyectado,
        "gastos_fijos": {"total": total_fijos, "detalle": detalle_fijos},
        "cuotas_credito": {"total": total_cuotas, "cantidad": len(tarjetas)},
        "gastos_variables_estimados": gastos_variables_estimados,
        "total_proyectado": total_proyectado,
    }


def compare_months(mes1: str, mes2: str) -> dict:
    todos_ingresos = sheets.get_all_rows(TAB_INGRESOS)

    def _stats(mes: str) -> dict:
        gastos = sheets.get_gastos_for_month(mes)
        ingresos = [i for i in todos_ingresos if str(i.get("fecha", "")).startswith(mes)]
        total_gastos = sum(safe_float(g.get("monto")) for g in gastos)
        total_ingresos = sum(safe_float(i.get("monto")) for i in ingresos)
        por_categoria: dict = {}
        for g in gastos:
            cat = str(g.get("categoria", "") or "sin_categoria")
            por_categoria[cat] = por_categoria.get(cat, 0) + safe_float(g.get("monto"))
        return {
            "total_gastos": total_gastos,
            "total_ingresos": total_ingresos,
            "saldo": total_ingresos - total_gastos,
            "n_transacciones": len(gastos),
            "por_categoria": por_categoria,
        }

    stats1 = _stats(mes1)
    stats2 = _stats(mes2)

    return {
        "ok": True,
        mes1: stats1,
        mes2: stats2,
        "diferencia": {
            "gastos": stats2["total_gastos"] - stats1["total_gastos"],
            "ingresos": stats2["total_ingresos"] - stats1["total_ingresos"],
            "saldo": stats2["saldo"] - stats1["saldo"],
        },
    }


def get_installments_ending_soon(remaining: int = 2) -> dict:
    tarjetas = sheets.get_all_rows(TAB_TARJETAS)
    por_terminar = []
    for t in tarjetas:
        try:
            cuota_actual = int(t.get("cuota_actual", 0))
            cuotas_total = int(t.get("cuotas_total", 0))
        except (ValueError, TypeError):
            continue
        cuotas_restantes = cuotas_total - cuota_actual
        if cuotas_restantes <= remaining:
            item = dict(t)
            item["cuotas_restantes"] = cuotas_restantes
            por_terminar.append(item)

    return {
        "ok": True,
        "por_terminar": por_terminar,
        "cantidad": len(por_terminar),
    }


def recalculate_all(personas: list[str] | None = None) -> dict:
    """Recalcula totales del mes actual para cada persona."""
    mes = datetime.now().strftime("%Y-%m")
    resultados = {}

    targets = personas or []
    if not targets:
        # Obtener personas únicas de los gastos del mes
        gastos = sheets.get_all_rows(TAB_GASTOS)
        targets = list({g.get("persona", "") for g in gastos if g.get("persona")})

    for persona in targets:
        try:
            s = get_monthly_summary(persona=persona, mes=mes)
            resultados[persona] = {
                "total_gastos": s["total_gastos"],
                "total_ingresos": s["total_ingresos"],
                "saldo": s["saldo"],
                "deuda_tarjetas": s["deuda_tarjetas"],
            }
        except Exception as e:
            resultados[persona] = f"error: {e}"

    return {"ok": True, "mes": mes, "resultados": resultados}
