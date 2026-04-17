"""
tools_archive.py — Archivado mensual: Gastos → Historico + fila de Resumen.

Al ejecutar /migrar:
1. Mueve todas las filas de Gastos de meses anteriores a Historico.
2. Calcula y escribe una fila de resumen en la pestaña Resumen para cada mes archivado.
3. Genera un carryover (deuda o saldo) al mes nuevo.
"""

import uuid
from collections import defaultdict
from datetime import datetime

import sheets
from sheets import safe_float
from config import (
    TAB_GASTOS, TAB_INGRESOS, TAB_HISTORICO, TAB_RESUMEN, TAB_AHORROS,
    HEADERS_GASTOS, HEADERS_INGRESOS, MESES_ES,
)


def _build_resumen_row(mes_key: str, gastos_rows: list, ingresos_rows: list, ahorros_rows: list) -> list:
    ingreso_total = round(sum(safe_float(r.get("monto")) for r in ingresos_rows), 2)
    gastos_tarjeta = round(
        sum(safe_float(r.get("monto")) for r in gastos_rows if r.get("metodo_pago") == "tarjeta_credito"), 2
    )
    gastos_manuales = round(
        sum(safe_float(r.get("monto")) for r in gastos_rows if r.get("metodo_pago") != "tarjeta_credito"), 2
    )
    gastos_total = round(gastos_tarjeta + gastos_manuales, 2)
    ahorro = round(
        sum(safe_float(r.get("monto_ars")) for r in ahorros_rows if str(r.get("fecha", "")).startswith(mes_key)), 2
    )
    saldo = round(ingreso_total - gastos_total, 2)

    return [mes_key, ingreso_total, gastos_total, ahorro, saldo]


def maybe_archive_past_months(carryover_persona: str = "Usuario") -> dict:
    """
    Archiva en Historico todas las filas de Gastos que no son del mes actual.
    Retorna estadísticas del archivado y el carryover generado.
    """
    current_mes = datetime.now().strftime("%Y-%m")

    all_gastos   = sheets.get_all_rows(TAB_GASTOS)
    all_ingresos = sheets.get_all_rows(TAB_INGRESOS)
    all_ahorros  = sheets.get_all_rows(TAB_AHORROS)

    old_gastos = [g for g in all_gastos if not str(g.get("fecha", "")).startswith(current_mes)]
    if not old_gastos:
        return {"archivados": 0, "meses": [], "carryover": {"tipo": "cero", "monto": 0}}

    gastos_by_month: dict[str, list] = defaultdict(list)
    ingresos_by_month: dict[str, list] = defaultdict(list)

    for row in old_gastos:
        gastos_by_month[str(row.get("fecha", ""))[:7]].append(row)
    for row in all_ingresos:
        mk = str(row.get("fecha", ""))[:7]
        if mk != current_mes:
            ingresos_by_month[mk].append(row)

    meses_archivados = sorted(set(gastos_by_month.keys()) | set(ingresos_by_month.keys()))

    # 1. Escribir en Historico
    historico_rows = []
    for mes_key in meses_archivados:
        try:
            año = int(mes_key[:4])
            mes_num = int(mes_key[5:7])
        except ValueError:
            continue
        mes_nombre = MESES_ES.get(mes_num, mes_key)
        for r in gastos_by_month.get(mes_key, []):
            historico_rows.append([año, mes_nombre] + [r.get(h, "") for h in HEADERS_GASTOS])

    if historico_rows:
        sheets.append_rows(TAB_HISTORICO, historico_rows)

    # 2. Escribir filas de Resumen (sin sobreescribir existentes)
    resumen_existente = {str(r.get("mes")) for r in sheets.get_all_rows(TAB_RESUMEN)}
    for mes_key in meses_archivados:
        if mes_key in resumen_existente:
            continue
        fila = _build_resumen_row(
            mes_key,
            gastos_rows=gastos_by_month.get(mes_key, []),
            ingresos_rows=ingresos_by_month.get(mes_key, []),
            ahorros_rows=all_ahorros,
        )
        sheets.append_row(TAB_RESUMEN, fila)

    # 3. Limpiar Gastos e Ingresos (conservar solo headers)
    ws_gastos   = sheets.get_worksheet(TAB_GASTOS)
    ws_ingresos = sheets.get_worksheet(TAB_INGRESOS)
    ws_gastos.clear()
    ws_gastos.append_row(HEADERS_GASTOS)
    ws_ingresos.clear()
    ws_ingresos.append_row(HEADERS_INGRESOS)
    # Invalida cache de worksheets para estas tabs
    sheets.invalidate_cache(sheets.get_active_sheet())

    # 4. Calcular carryover
    total_ingresos = sum(safe_float(r.get("monto")) for rows in ingresos_by_month.values() for r in rows)
    total_gastos   = sum(safe_float(r.get("monto")) for rows in gastos_by_month.values() for r in rows)
    saldo = round(total_ingresos - total_gastos, 2)
    hoy = datetime.now().strftime("%Y-%m-%d")

    if saldo < 0:
        gasto_id = uuid.uuid4().hex[:8]
        sheets.append_row(TAB_GASTOS, [
            gasto_id, hoy, carryover_persona, "Deuda mes pasado", abs(saldo),
            "efectivo", "", "", "", "", "carryover", "servicios",
        ])
        carryover = {"tipo": "deuda", "monto": abs(saldo)}
    elif saldo > 0:
        ingreso_id = uuid.uuid4().hex[:8]
        sheets.append_row(TAB_INGRESOS, [
            ingreso_id, hoy, carryover_persona, "variable", saldo, "Saldo acumulado",
        ])
        carryover = {"tipo": "saldo", "monto": saldo}
    else:
        carryover = {"tipo": "cero", "monto": 0}

    return {
        "archivados": len(old_gastos),
        "meses": meses_archivados,
        "carryover": carryover,
    }
