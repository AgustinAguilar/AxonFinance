"""
sheets.py — Capa de acceso a Google Sheets, multi-tenant.

Usa contextvars.ContextVar para saber qué planilla está activa en cada
request (seguro para asyncio: cada tarea tiene su propia copia del contexto).

Uso típico en bot.py / tools.py:
    sheets.set_active_sheet(user["sheet_id"])
    # ... luego todas las funciones de tools usan get_active_sheet() automáticamente
"""

import contextvars
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import (
    GOOGLE_CREDENTIALS_FILE,
    TAB_GASTOS, TAB_INGRESOS, TAB_TARJETAS, TAB_RESUMEN, TAB_AHORROS,
    TAB_GASTOS_FIJOS, TAB_PRESTAMOS, TAB_HISTORICO,
    TAB_HEADERS, MESES_ES,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ContextVar: sheet_id activo para el request actual
_active_sheet_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_sheet_id", default=None
)

# Cache: {sheet_id: {tab_name: gspread.Worksheet}}
_worksheet_cache: dict[str, dict[str, gspread.Worksheet]] = {}

# Singleton del cliente gspread (una sola autorización)
_client: gspread.Client | None = None


def set_active_sheet(sheet_id: str) -> None:
    _active_sheet_id.set(sheet_id)


def get_active_sheet() -> str:
    val = _active_sheet_id.get()
    if not val:
        raise RuntimeError("No hay planilla activa. Llamá set_active_sheet() primero.")
    return val


def safe_float(value, default: float = 0.0) -> float:
    if value == "" or value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _get_spreadsheet(sheet_id: str) -> gspread.Spreadsheet:
    return _get_client().open_by_key(sheet_id)


def get_worksheet(tab_name: str) -> gspread.Worksheet:
    sheet_id = get_active_sheet()
    if sheet_id not in _worksheet_cache:
        _worksheet_cache[sheet_id] = {}
    cache = _worksheet_cache[sheet_id]

    if tab_name in cache:
        return cache[tab_name]

    ss = _get_spreadsheet(sheet_id)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=1000, cols=20)
        headers = TAB_HEADERS.get(tab_name, [])
        if headers:
            ws.append_row(headers)
    cache[tab_name] = ws
    return ws


def init_sheets() -> None:
    """Crea todas las tabs con headers si no existen (usa planilla activa)."""
    for tab_name in TAB_HEADERS:
        get_worksheet(tab_name)


def append_row(tab_name: str, row: list) -> None:
    ws = get_worksheet(tab_name)
    ws.append_row(row, value_input_option="RAW")


def append_rows(tab_name: str, rows: list[list]) -> None:
    if not rows:
        return
    ws = get_worksheet(tab_name)
    ws.append_rows(rows, value_input_option="RAW")


def get_all_rows(tab_name: str) -> list[dict]:
    ws = get_worksheet(tab_name)
    return ws.get_all_records(value_render_option="UNFORMATTED_VALUE")


def find_rows(tab_name: str, **filters) -> list[dict]:
    rows = get_all_rows(tab_name)
    return [
        row for row in rows
        if all(str(row.get(k, "")) == str(v) for k, v in filters.items() if v is not None)
    ]


def delete_row_by_id(tab_name: str, id_column: str, id_value: str) -> bool:
    ws = get_worksheet(tab_name)
    records = ws.get_all_values()
    if not records:
        return False
    headers = records[0]
    try:
        col_idx = headers.index(id_column)
    except ValueError:
        return False
    for i, row in enumerate(records[1:], start=2):
        if len(row) > col_idx and row[col_idx] == id_value:
            ws.delete_rows(i)
            return True
    return False


def update_cell(tab_name: str, row_index: int, col_name: str, value) -> None:
    ws = get_worksheet(tab_name)
    headers = ws.row_values(1)
    col_idx = headers.index(col_name) + 1
    ws.update_cell(row_index, col_idx, value)


def get_gastos_for_month(mes: str) -> list[dict]:
    """
    Devuelve gastos para YYYY-MM desde la fuente correcta:
    - Mes actual → TAB_GASTOS
    - Mes pasado → TAB_HISTORICO (filtrado por año + nombre de mes)
    """
    if not re.match(r'^\d{4}-\d{2}$', mes):
        return []

    current_mes = datetime.now().strftime("%Y-%m")
    if mes == current_mes:
        return get_all_rows(TAB_GASTOS)

    año = int(mes[:4])
    mes_num = int(mes[5:7])
    if mes_num < 1 or mes_num > 12:
        return []
    mes_nombre = MESES_ES[mes_num]
    historico = get_all_rows(TAB_HISTORICO)
    return [r for r in historico if r.get("año") == año and r.get("mes") == mes_nombre]


def invalidate_cache(sheet_id: str | None = None) -> None:
    """Limpia la cache de worksheets (útil si se crean/eliminan tabs)."""
    if sheet_id:
        _worksheet_cache.pop(sheet_id, None)
    else:
        _worksheet_cache.clear()


# ─── Creación de planilla nueva ────────────────────────────────────────────────

def create_user_spreadsheet(owner_name: str, owner_email: str | None = None) -> tuple[str, str]:
    """
    Crea una planilla de Google Sheets nueva para el usuario.
    - Crea todas las tabs con sus headers.
    - Si se pasa owner_email, comparte la planilla con ese email (editor).
    Retorna (sheet_id, sheet_url).
    """
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    sheets_service = build("sheets", "v4", credentials=creds)

    # Crear spreadsheet con todas las tabs
    body = {
        "properties": {"title": f"Axon Finance — {owner_name}"},
        "sheets": [
            {"properties": {"title": tab_name, "index": i}}
            for i, tab_name in enumerate(TAB_HEADERS.keys())
        ],
    }
    result = sheets_service.spreadsheets().create(body=body).execute()
    sheet_id = result["spreadsheetId"]
    sheet_url = result["spreadsheetUrl"]

    # Escribir headers en cada tab con batchUpdate
    data = []
    for tab_name, headers in TAB_HEADERS.items():
        data.append({
            "range": f"'{tab_name}'!A1",
            "values": [headers],
        })

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()

    # Compartir con el email del usuario
    if owner_email:
        drive_service = build("drive", "v3", credentials=creds)
        drive_service.permissions().create(
            fileId=sheet_id,
            body={
                "type": "user",
                "role": "writer",
                "emailAddress": owner_email,
            },
            sendNotificationEmail=True,
            emailMessage=(
                f"¡Hola {owner_name}! Tu planilla de Axon Finance está lista. "
                "Guardá este link para acceder siempre a tus finanzas."
            ),
        ).execute()

    return sheet_id, sheet_url
