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
    TAB_DASHBOARD, TAB_GASTOS, TAB_INGRESOS, TAB_TARJETAS, TAB_RESUMEN, TAB_AHORROS,
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

def rebuild_dashboard_for_phone(phone: str, sheet_id: str,
                                personas: list[str], tarjetas: list[str]) -> None:
    """
    (Re)genera la pestaña Dashboard en una planilla existente. Sirve para
    migrar planillas viejas (sin dashboard) o si cambiaron personas/tarjetas.
    Crea la tab si no existe, la limpia y escribe la grilla de fórmulas.
    """
    import oauth as oauth_module
    from googleapiclient.discovery import build

    creds = oauth_module.get_credentials_for_phone(phone)
    if not creds:
        raise RuntimeError(f"Sin credenciales OAuth para phone={phone}.")

    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Ver si ya existe la tab Dashboard
    ss_meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {
        s.get("properties", {}).get("title", ""): s["properties"]["sheetId"]
        for s in ss_meta.get("sheets", [])
    }

    if TAB_DASHBOARD not in existing:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {"addSheet": {"properties": {"title": TAB_DASHBOARD, "index": 0}}}
            ]},
        ).execute()
    else:
        # Limpiar contenido previo
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"'{TAB_DASHBOARD}'!A1:Z200",
        ).execute()

    grid = _build_dashboard_grid(personas, tarjetas)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{TAB_DASHBOARD}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": grid},
    ).execute()


def _build_dashboard_grid(personas: list[str], tarjetas: list[str]) -> list[list]:
    """
    Construye la grilla del Dashboard con fórmulas parametrizadas por las personas
    y tarjetas del cliente. Retorna una matriz 2D (filas × columnas) lista para
    mandar a Sheets API con valueInputOption=USER_ENTERED.

    Layout (columnas A..G):
      Título + mes
      RESUMEN GENERAL (A-C / D-F / G): Total Gastos | Total Ingresos | Saldo Neto
      GASTOS POR PERSONA (A-B) | INGRESOS POR PERSONA (D-E)
      GASTOS POR METODO (A-B) | DEUDA TARJETAS (D-E)
                              | GASTOS FIJOS / MES (D-E)
      AHORROS ACUMULADOS (A-C)
      AHORROS POR TIPO (A-B)
    """
    personas = personas or ["Usuario"]
    # Matriz usada como dict {(row, col): value} → luego se rellena en rectángulo
    cells: dict[tuple[int, int], str] = {}

    def put(row: int, col: int, val) -> None:
        cells[(row, col)] = val

    # Header
    put(0, 0, "💰 Axon Finance — Dashboard Financiero")
    put(1, 0, '=TEXT(TODAY(),"mmmm yyyy")')

    # ── RESUMEN GENERAL (fila 4) ──
    put(3, 0, "RESUMEN GENERAL")
    put(4, 0, "Total Gastos")
    put(4, 3, "Total Ingresos")
    put(4, 6, "Saldo Neto")
    put(5, 0, "=SUM(Gastos!E:E)")
    put(5, 3, "=SUM(Ingresos!E:E)")
    put(5, 6, "=SUM(Ingresos!E:E)-SUM(Gastos!E:E)")

    # ── GASTOS POR PERSONA (col A-B) | INGRESOS POR PERSONA (col D-E) ──
    put(7, 0, "GASTOS POR PERSONA")
    put(7, 3, "INGRESOS POR PERSONA")
    put(8, 0, "Persona"); put(8, 1, "Total")
    put(8, 3, "Persona"); put(8, 4, "Total")
    for i, p in enumerate(personas):
        r = 9 + i
        put(r, 0, p)
        put(r, 1, f'=SUMIF(Gastos!C:C,"{p}",Gastos!E:E)')
        put(r, 3, p)
        put(r, 4, f'=SUMIF(Ingresos!C:C,"{p}",Ingresos!E:E)')
    tot_p_row = 9 + len(personas)
    put(tot_p_row, 0, "TOTAL")
    put(tot_p_row, 1, f"=SUM(B10:B{tot_p_row})")
    put(tot_p_row, 3, "TOTAL")
    put(tot_p_row, 4, f"=SUM(E10:E{tot_p_row})")

    # ── GASTOS POR METODO DE PAGO (col A-B) ──
    metodo_start = tot_p_row + 2
    put(metodo_start, 0, "GASTOS POR METODO DE PAGO")
    put(metodo_start + 1, 0, "Metodo"); put(metodo_start + 1, 1, "Total")
    metodo_rows = [
        ("Efectivo", '=SUMIF(Gastos!F:F,"efectivo",Gastos!E:E)'),
    ]
    for t in tarjetas:
        metodo_rows.append((
            f"Tarjeta {t}",
            f'=SUMPRODUCT((Gastos!F2:F5000="tarjeta_credito")*(Gastos!G2:G5000="{t}")*Gastos!E2:E5000)',
        ))
    metodo_rows += [
        ("MercadoPago", '=SUMIF(Gastos!F:F,"mercadopago",Gastos!E:E)'),
        ("Mercado Credito", '=SUMIF(Gastos!F:F,"mercado_credito",Gastos!E:E)'),
    ]
    for i, (label, formula) in enumerate(metodo_rows):
        r = metodo_start + 2 + i
        put(r, 0, label)
        put(r, 1, formula)
    metodo_total_row = metodo_start + 2 + len(metodo_rows)
    put(metodo_total_row, 0, "TOTAL")
    put(metodo_total_row, 1, f"=SUM(B{metodo_start + 3}:B{metodo_total_row})")

    # ── DEUDA TARJETAS (col D-E) — empieza en la misma fila que metodo ──
    deuda_start = metodo_start
    put(deuda_start, 3, "DEUDA TARJETAS (PENDIENTE)")
    put(deuda_start + 1, 3, "Tarjeta"); put(deuda_start + 1, 4, "Total")
    if tarjetas:
        for i, t in enumerate(tarjetas):
            r = deuda_start + 2 + i
            put(r, 3, t)
            put(r, 4, f'=SUMIF(Tarjetas!B:B,"{t}",Tarjetas!F:F)')
        deuda_total_row = deuda_start + 2 + len(tarjetas)
        put(deuda_total_row, 3, "TOTAL")
        put(deuda_total_row, 4, f"=SUM(E{deuda_start + 3}:E{deuda_total_row})")
    else:
        put(deuda_start + 2, 3, "Sin tarjetas configuradas")

    # ── GASTOS FIJOS / MES (col D-E) — debajo de deuda tarjetas ──
    fijos_row = (deuda_start + 2 + max(len(tarjetas), 1)) + 2
    put(fijos_row, 3, "GASTOS FIJOS / MES")
    put(fijos_row + 1, 3, "Total estimado")
    put(fijos_row + 1, 4, "=SUMPRODUCT(N('Gastos Fijos'!F2:F200)*N('Gastos Fijos'!B2:B200))")

    # ── AHORROS ACUMULADOS (col A-C) — debajo de gastos por método ──
    ahorros_start = metodo_total_row + 2
    put(ahorros_start, 0, "AHORROS ACUMULADOS")
    put(ahorros_start + 1, 0, "Persona")
    put(ahorros_start + 1, 1, "ARS")
    put(ahorros_start + 1, 2, "USD (blue)")
    for i, p in enumerate(personas):
        r = ahorros_start + 2 + i
        put(r, 0, p)
        put(r, 1, f'=SUMIF(Ahorros!B:B,"{p}",Ahorros!D:D)')
        put(r, 2, f'=SUMIF(Ahorros!B:B,"{p}",Ahorros!F:F)')
    ahorros_total = ahorros_start + 2 + len(personas)
    put(ahorros_total, 0, "TOTAL")
    put(ahorros_total, 1, f"=SUM(B{ahorros_start + 3}:B{ahorros_total})")
    put(ahorros_total, 2, f"=SUM(C{ahorros_start + 3}:C{ahorros_total})")

    # ── AHORROS POR TIPO (col A-B) ──
    tipos = ["Jubilacion", "Inversion Corto Plazo", "Ahorro Fisico", "Ahorro Virtual", "Crypto"]
    tipo_start = ahorros_total + 2
    put(tipo_start, 0, "AHORROS POR TIPO")
    put(tipo_start + 1, 0, "Tipo")
    put(tipo_start + 1, 1, "ARS")
    for i, t in enumerate(tipos):
        r = tipo_start + 2 + i
        put(r, 0, t)
        put(r, 1, f'=SUMIF(Ahorros!C:C,"{t}",Ahorros!D:D)')
    tipo_total = tipo_start + 2 + len(tipos)
    put(tipo_total, 0, "TOTAL")
    put(tipo_total, 1, f"=SUM(B{tipo_start + 3}:B{tipo_total})")

    # Renderizar a matriz rectangular (rows × cols)
    max_row = max(r for (r, _) in cells.keys())
    max_col = 7  # A..G
    grid = [["" for _ in range(max_col)] for _ in range(max_row + 1)]
    for (r, c), v in cells.items():
        grid[r][c] = v
    return grid


def create_user_spreadsheet_for_phone(
    phone: str,
    owner_name: str,
    personas: list[str] | None = None,
    tarjetas: list[str] | None = None,
) -> tuple[str, str]:
    """
    Crea una planilla de Google Sheets en el Drive del cliente (vía OAuth).
    - Usa Drive API files().create con mimeType spreadsheet → se crea en su Drive.
    - Scope drive.file → la app solo puede acceder a este archivo (no al resto del Drive).
    - Crea la pestaña Dashboard (con fórmulas) + tabs de datos con headers.
    - Al ser scope drive.file + creador=usuario, el usuario ya es dueño.
    Retorna (sheet_id, sheet_url).
    """
    import oauth as oauth_module
    from googleapiclient.discovery import build

    creds = oauth_module.get_credentials_for_phone(phone)
    if not creds:
        raise RuntimeError(f"Sin credenciales OAuth para phone={phone}.")

    personas = personas or [owner_name]
    tarjetas = tarjetas or []

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

    # 2. Crear las tabs vía Sheets API (Dashboard primero, luego tabs de datos)
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    tab_order = [TAB_DASHBOARD] + list(TAB_HEADERS.keys())
    add_sheet_requests = [
        {"addSheet": {"properties": {"title": name, "index": i}}}
        for i, name in enumerate(tab_order)
    ]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": add_sheet_requests},
    ).execute()

    # 3. Escribir contenido:
    #    - Dashboard: grilla con fórmulas (USER_ENTERED para que las interprete)
    #    - Tabs de datos: headers (RAW)
    dashboard_grid = _build_dashboard_grid(personas, tarjetas)

    # Dashboard va con USER_ENTERED (para fórmulas); el resto con RAW.
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{TAB_DASHBOARD}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": dashboard_grid},
    ).execute()

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
        known = set(tab_order)
        default_sheet = None
        for s in ss_meta.get("sheets", []):
            title = s.get("properties", {}).get("title", "")
            if title not in known:
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
