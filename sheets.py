"""
sheets.py — Capa de acceso a Google Sheets, multi-tenant con OAuth por usuario.

Cada request usa las credenciales OAuth del usuario activo (obtenidas de
users.json vía oauth.get_credentials_for_phone). Las planillas se crean
en el Drive del propio usuario, con scope drive.file (solo archivos creados
por la app).
"""

import contextvars
import logging
import re
from datetime import datetime

import gspread

from config import (
    TAB_GASTOS, TAB_INGRESOS, TAB_TARJETAS, TAB_RESUMEN, TAB_AHORROS,
    TAB_GASTOS_FIJOS, TAB_PRESTAMOS, TAB_HISTORICO,
    TAB_HEADERS, MESES_ES,
)

logger = logging.getLogger(__name__)

# ContextVars: usuario activo (phone) y planilla activa (sheet_id)
_active_phone: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_phone", default=None
)
_active_sheet_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_sheet_id", default=None
)

# Cache: {phone: gspread.Client} y {sheet_id: {tab_name: gspread.Worksheet}}
_client_cache: dict[str, gspread.Client] = {}
_worksheet_cache: dict[str, dict[str, gspread.Worksheet]] = {}


def set_active_user(phone: str, sheet_id: str) -> None:
    """Setea el usuario y la planilla activa para el request actual."""
    _active_phone.set(phone)
    _active_sheet_id.set(sheet_id)


def get_active_phone() -> str:
    val = _active_phone.get()
    if not val:
        raise RuntimeError("No hay usuario activo. Llamá set_active_user() primero.")
    return val


def get_active_sheet() -> str:
    val = _active_sheet_id.get()
    if not val:
        raise RuntimeError("No hay planilla activa. Llamá set_active_user() primero.")
    return val


def safe_float(value, default: float = 0.0) -> float:
    if value == "" or value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _get_client() -> gspread.Client:
    """Retorna gspread client con OAuth del usuario activo."""
    import oauth as oauth_module
    phone = get_active_phone()
    creds = oauth_module.get_credentials_for_phone(phone)
    if not creds:
        raise RuntimeError(f"Sin credenciales OAuth para phone={phone}. Reconectar Google.")
    # Cache por phone (invalidar si se refresca el token)
    cached = _client_cache.get(phone)
    if cached and cached.auth and cached.auth.token == creds.token:
        return cached
    client = gspread.authorize(creds)
    _client_cache[phone] = client
    return client


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


# ─── Creación de planilla nueva en el Drive del usuario ────────────────────────

def create_user_spreadsheet_for_phone(phone: str, owner_name: str) -> tuple[str, str]:
    """
    Crea una planilla de Google Sheets en el Drive del cliente (vía OAuth).
    - Usa Drive API files().create con mimeType spreadsheet → se crea en su Drive.
    - Scope drive.file → la app solo puede acceder a este archivo (no al resto del Drive).
    - Crea todas las tabs con sus headers vía Sheets API.
    - Al ser scope drive.file + creador=usuario, el usuario ya es dueño. No requiere compartir.
    Retorna (sheet_id, sheet_url).
    """
    import oauth as oauth_module
    from googleapiclient.discovery import build

    creds = oauth_module.get_credentials_for_phone(phone)
    if not creds:
        raise RuntimeError(f"Sin credenciales OAuth para phone={phone}.")

    # 1. Crear el archivo vía Drive API (queda en el Drive del usuario)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    file_metadata = {
        "name": f"Axon Finance — {owner_name}",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    created = drive_service.files().create(
        body=file_metadata,
        fields="id, webViewLink",
    ).execute()
    sheet_id = created["id"]
    sheet_url = created.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    # 2. Crear las tabs + escribir headers vía Sheets API
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    tab_names = list(TAB_HEADERS.keys())
    # Sheets API: batchUpdate para agregar las tabs nuevas (la default "Hoja 1" la dejamos al final para borrar)
    add_sheet_requests = [
        {"addSheet": {"properties": {"title": name, "index": i}}}
        for i, name in enumerate(tab_names)
    ]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": add_sheet_requests},
    ).execute()

    # 3. Escribir headers en cada tab
    data = [
        {"range": f"'{name}'!A1", "values": [headers]}
        for name, headers in TAB_HEADERS.items()
    ]
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()

    # 4. Borrar la "Hoja 1" default que vino con el archivo
    try:
        ss_meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        default_sheet = None
        for s in ss_meta.get("sheets", []):
            title = s.get("properties", {}).get("title", "")
            if title not in TAB_HEADERS:
                default_sheet = s["properties"]["sheetId"]
                break
        if default_sheet is not None:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": default_sheet}}]},
            ).execute()
    except Exception as e:
        logger.warning("No se pudo borrar la hoja default: %s", e)

    return sheet_id, sheet_url
