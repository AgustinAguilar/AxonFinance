"""
tools_pdf.py — Importación de resúmenes de tarjeta de crédito en PDF.

Bancos soportados: Santander, BBVA, Galicia, ICBC.
La extracción de transacciones la hace Claude (funciona con cualquier banco
siempre que el PDF tenga texto seleccionable). La conversión USD→ARS la hace
Python, nunca el LLM.
"""

import re
import uuid
import json
from datetime import datetime

import pdfplumber
import anthropic

from config import TAB_GASTOS, TAB_TARJETAS, ANTHROPIC_API_KEY
from tools_dashboard import get_dolar_blue
import sheets


def _normalize_desc(desc: str) -> str:
    """Elimina códigos de transacción al final para matching entre meses."""
    cleaned = re.sub(r'\s+[A-Z0-9]{6,}$', '', desc.strip(), flags=re.IGNORECASE)
    return cleaned.strip().lower()


def _upsert_tarjeta(descripcion: str, tarjeta: str, persona: str,
                    cuota_actual: int, cuotas_total: int, monto_cuota: float) -> str:
    """
    Upsert de una compra en cuotas en la pestaña Tarjetas.
    Retorna: "creada" | "actualizada" | "eliminada" | "ignorada"
    """
    ws = sheets.get_worksheet(TAB_TARJETAS)
    all_values = ws.get_all_values()
    if not all_values:
        if cuota_actual < cuotas_total:
            sheets.append_row(TAB_TARJETAS, [
                descripcion, tarjeta, persona, cuota_actual, cuotas_total, monto_cuota,
            ])
            return "creada"
        return "ignorada"

    headers = all_values[0]
    try:
        desc_col  = headers.index("descripcion")
        tarj_col  = headers.index("tarjeta")
        cuota_col = headers.index("cuota_actual")
        total_col = headers.index("cuotas_total")
        monto_col = headers.index("monto_cuota")
    except ValueError:
        return "ignorada"

    needle = _normalize_desc(descripcion)
    for i, row in enumerate(all_values[1:], start=2):
        if len(row) <= max(desc_col, tarj_col, total_col):
            continue
        if (row[tarj_col].upper() == tarjeta.upper()
                and str(row[total_col]) == str(cuotas_total)
                and _normalize_desc(row[desc_col]) == needle):
            if cuota_actual >= cuotas_total:
                ws.delete_rows(i)
                return "eliminada"
            else:
                ws.update_cell(i, cuota_col + 1, cuota_actual)
                ws.update_cell(i, monto_col + 1, monto_cuota)
                return "actualizada"

    if cuota_actual < cuotas_total:
        sheets.append_row(TAB_TARJETAS, [
            descripcion, tarjeta, persona, cuota_actual, cuotas_total, monto_cuota,
        ])
        return "creada"
    return "ignorada"


def parse_credit_card_pdf(pdf_path: str, tarjeta: str, persona: str) -> dict:
    """Extrae transacciones de un PDF de resumen de tarjeta y las registra en la planilla."""
    try:
        text = _extract_text(pdf_path)
    except Exception as e:
        return {"ok": False, "error": f"No se pudo leer el PDF: {e}"}

    if not text.strip():
        return {"ok": False, "error": "El PDF no tiene texto legible. Verificá que no sea una imagen escaneada."}

    dolar = get_dolar_blue()
    dolar_venta = dolar.get("venta", 0) if dolar.get("ok") else 0
    texto_truncado = len(text) > 15000

    transactions = _parse_with_claude(text, tarjeta)
    if not transactions:
        return {"ok": False, "error": "No se pudieron extraer transacciones del PDF. Verificá que sea un resumen de tarjeta válido."}

    gastos_rows = []
    tarjetas_stats = {"creada": 0, "actualizada": 0, "eliminada": 0}
    reembolsos_count = 0
    now = datetime.now()

    for tx in transactions:
        try:
            gasto_id     = uuid.uuid4().hex[:8]
            fecha        = now.strftime("%Y-%m-%d")
            descripcion  = tx.get("descripcion", "Sin descripcion")
            cuotas       = max(1, int(tx.get("cuotas", 1)))
            cuota_actual = max(1, int(tx.get("cuota_actual", 1)))
            tipo         = tx.get("tipo", "compra")
            moneda       = tx.get("moneda_original", "ARS")

            monto_raw = round(float(tx.get("monto", 0)), 2)
            if moneda == "USD":
                monto_cuota = round(monto_raw * dolar_venta, 2) if dolar_venta > 0 else 0.0
            else:
                monto_cuota = monto_raw

            if tipo == "reembolso":
                gastos_rows.append([
                    gasto_id, fecha, persona, descripcion, -abs(monto_cuota),
                    "tarjeta_credito", tarjeta, "", "", "", "reembolso", "",
                ])
                reembolsos_count += 1
                continue

            gastos_rows.append([
                gasto_id, fecha, persona, descripcion, monto_cuota,
                "tarjeta_credito", tarjeta,
                cuotas if cuotas > 1 else "",
                cuota_actual if cuotas > 1 else "",
                monto_cuota if cuotas > 1 else "",
                "pdf_import", "",
            ])

            if cuotas > 1:
                resultado = _upsert_tarjeta(descripcion, tarjeta, persona,
                                            cuota_actual, cuotas, monto_cuota)
                if resultado in tarjetas_stats:
                    tarjetas_stats[resultado] += 1

        except Exception:
            continue

    if gastos_rows:
        sheets.append_rows(TAB_GASTOS, gastos_rows)

    usd_count = sum(1 for tx in transactions if tx.get("moneda_original") == "USD")
    msg = f"Se importaron {len(gastos_rows)} transacciones de {tarjeta}."
    if usd_count and dolar_venta > 0:
        msg += f" {usd_count} en USD convertidos al blue (${dolar_venta:,.0f})."
    elif usd_count and not dolar_venta:
        msg += f" ADVERTENCIA: {usd_count} transacciones en USD quedaron en $0 (no se pudo obtener el dólar blue). Corregirlas manualmente."
    if reembolsos_count:
        msg += f" {reembolsos_count} reembolso(s) registrado(s) con monto negativo."
    if any(tarjetas_stats.values()):
        msg += (
            f" Cuotas: {tarjetas_stats['creada']} nuevas, "
            f"{tarjetas_stats['actualizada']} actualizadas, "
            f"{tarjetas_stats['eliminada']} finalizadas."
        )
    if texto_truncado:
        msg += " AVISO: El PDF era muy largo y se procesó parcialmente. Verificá que no falten transacciones."

    return {
        "ok": True,
        "mensaje": msg,
        "transacciones": len(gastos_rows),
        "tarjetas": tarjetas_stats,
        "dolar_blue_usado": dolar_venta,
    }


def _extract_text(pdf_path: str) -> str:
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def _parse_with_claude(text: str, tarjeta: str) -> list[dict]:
    """
    Extrae transacciones del texto del PDF usando Claude.
    Solo parsea — NO convierte monedas (eso lo hace Python).
    """
    TEXT_LIMIT = 15000
    texto_truncado = len(text) > TEXT_LIMIT
    texto_resumen = text[:TEXT_LIMIT]

    truncado_aviso = (
        "\nNOTA: El texto fue truncado por longitud. Procesá las transacciones visibles."
        if texto_truncado else ""
    )

    prompt = f"""Analizá este texto de un resumen de tarjeta de crédito ({tarjeta}) y extraé todas las transacciones.

Para cada transacción devolvé un JSON con:
- "fecha": fecha de la compra en formato YYYY-MM-DD
- "descripcion": descripción de la compra
- "monto": importe de ESA CUOTA TAL COMO APARECE en el resumen (número positivo, sin símbolos ni letras).
  No conviertas monedas. Si dice "USD 45.00", monto=45.00. Si dice "$30.000", monto=30000.
- "cuotas": cantidad TOTAL de cuotas (1 si es pago único)
- "cuota_actual": número de cuota que aparece en ESTE resumen (1 si es pago único)
- "moneda_original": "USD" si el importe estaba en dólares, "ARS" si estaba en pesos
- "tipo": "compra" para consumos normales, "reembolso" para devoluciones/reintegros/créditos a favor

Ejemplos de cuotas: "3/12" → cuota_actual=3, cuotas=12. "CTA 2/6" → cuota_actual=2, cuotas=6.
Incluí reembolsos con tipo="reembolso" y monto positivo (el sistema lo convierte a negativo).
No incluyas pagos del resumen ni el total, solo compras y reembolsos individuales.
Devolvé SOLO un array JSON válido, sin texto adicional ni markdown.
{truncado_aviso}

Texto del resumen:
{texto_resumen}"""

    try:
        response = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[1]
            result_text = result_text.rsplit("```", 1)[0]
        return json.loads(result_text)
    except Exception:
        return []
