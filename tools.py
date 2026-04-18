"""
tools.py — Definiciones de tools para Claude y ejecutor central.

Todas las tools son agnósticas a la identidad del usuario; reciben
la configuración del usuario como parámetro en execute_tool().
"""

import json
import logging

import tools_finance
import tools_dashboard
import tools_pdf
import tools_fixed
import tools_loans
from config import BANKS_SUPPORTED, CATEGORIES

logger = logging.getLogger(__name__)


TOOL_DEFINITIONS = [
    {
        "name": "add_expense",
        "description": "Registrar un gasto. Usar cuando el usuario menciona que gastó, compró o pagó algo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "descripcion": {"type": "string", "description": "Qué se compró/pagó"},
                "monto": {"type": "number", "description": "Monto en pesos argentinos"},
                "metodo_pago": {
                    "type": "string",
                    "enum": ["efectivo", "mercadopago", "tarjeta_credito", "mercado_credito"],
                },
                "tarjeta": {
                    "type": "string",
                    "description": "Nombre del banco emisor (ej: Santander, BBVA, Galicia). Solo si metodo_pago es tarjeta_credito.",
                },
                "cuotas": {
                    "type": "integer",
                    "description": "Cantidad de cuotas. 1 si es pago completo. Solo para tarjeta_credito.",
                },
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD. Omitir para hoy."},
                "categoria": {
                    "type": "string",
                    "enum": CATEGORIES,
                    "description": "Categoría del gasto. Inferirla automáticamente si no se especifica.",
                },
            },
            "required": ["descripcion", "monto", "metodo_pago"],
        },
    },
    {
        "name": "add_income",
        "description": "Registrar un ingreso (sueldo, cobro freelance, venta, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": {"type": "number", "description": "Monto en pesos"},
                "tipo": {
                    "type": "string",
                    "enum": ["salario", "variable"],
                    "description": "salario para sueldo fijo mensual, variable para ingresos puntuales",
                },
                "descripcion": {"type": "string", "description": "Descripción opcional"},
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD. Omitir para hoy."},
            },
            "required": ["monto", "tipo"],
        },
    },
    {
        "name": "get_expenses",
        "description": "Listar y filtrar gastos. Útil para consultas como '¿cuánto gasté este mes?' o '¿qué gasté en comida?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "persona": {"type": "string", "description": "Filtrar por persona. Omitir para todos."},
                "mes": {"type": "string", "description": "YYYY-MM para filtrar por mes específico"},
                "metodo_pago": {"type": "string"},
                "limit": {"type": "integer", "description": "Máximo de resultados. Default 20."},
            },
            "required": [],
        },
    },
    {
        "name": "delete_expense",
        "description": "Eliminar un gasto por ID o el último gasto registrado ('ultimo').",
        "input_schema": {
            "type": "object",
            "properties": {
                "gasto_id": {
                    "type": "string",
                    "description": "ID del gasto a eliminar, o 'ultimo' para el más reciente",
                },
            },
            "required": ["gasto_id"],
        },
    },
    {
        "name": "get_monthly_summary",
        "description": "Resumen financiero del mes: gastos, ingresos, deuda de tarjetas y saldo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "YYYY-MM. Omitir para el mes actual."},
                "persona": {"type": "string", "description": "Filtrar por persona. Omitir para todos."},
            },
            "required": [],
        },
    },
    {
        "name": "get_credit_card_debt",
        "description": "Ver cuotas pendientes por tarjeta de crédito y total a pagar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tarjeta": {"type": "string", "description": "Filtrar por banco (ej: Santander). Omitir para todos."},
                "persona": {"type": "string", "description": "Filtrar por persona. Omitir para todos."},
            },
            "required": [],
        },
    },
    {
        "name": "parse_credit_card_pdf",
        "description": (
            "Importar transacciones desde un resumen de tarjeta de crédito en PDF. "
            "Usar cuando el usuario envía un PDF de resumen de tarjeta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tarjeta": {
                    "type": "string",
                    "enum": BANKS_SUPPORTED,
                    "description": "Banco emisor del resumen",
                },
            },
            "required": ["tarjeta"],
        },
    },
    {
        "name": "get_dolar_blue",
        "description": "Obtener cotización actual del dólar blue (compra y venta).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "pay_fixed_expense",
        "description": (
            "Registrar el pago de un gasto fijo del catálogo (alquiler, luz, gas, internet, etc.). "
            "Usar cuando el usuario dice 'pagué el alquiler', 'pagué la luz', etc. "
            "Si menciona un monto nuevo, pasarlo para actualizar el catálogo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Nombre del gasto fijo (ej: 'Alquiler', 'Luz', 'Internet')",
                },
                "monto": {
                    "type": "number",
                    "description": "Monto pagado. Omitir para usar el último monto registrado.",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "get_fixed_expenses_status",
        "description": (
            "Estado de los gastos fijos del mes: cuáles se pagaron, cuáles faltan, "
            "total estimado vs pagado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "YYYY-MM. Omitir para el mes actual."},
            },
            "required": [],
        },
    },
    {
        "name": "add_fixed_expense",
        "description": "Agregar un nuevo gasto fijo al catálogo (ej: 'agregá el gimnasio como gasto fijo').",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string"},
                "monto_estimado": {"type": "number", "description": "Monto mensual estimado"},
                "metodo_pago": {
                    "type": "string",
                    "enum": ["efectivo", "mercadopago", "tarjeta_credito"],
                },
                "tarjeta": {"type": "string", "description": "Banco. Solo si metodo_pago es tarjeta_credito."},
                "origen": {
                    "type": "string",
                    "enum": ["manual", "tarjeta"],
                    "description": "'manual' si se paga directamente. 'tarjeta' si viene del PDF.",
                },
            },
            "required": ["nombre", "monto_estimado", "metodo_pago"],
        },
    },
    {
        "name": "get_next_month_projection",
        "description": "Proyección de gastos del mes siguiente: gastos fijos + cuotas pendientes + promedio de variables.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "compare_months",
        "description": "Comparar dos meses: gastos, ingresos, saldo y diferencias por categoría.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes1": {"type": "string", "description": "YYYY-MM primer mes"},
                "mes2": {"type": "string", "description": "YYYY-MM segundo mes"},
            },
            "required": ["mes1", "mes2"],
        },
    },
    {
        "name": "get_installments_ending_soon",
        "description": "Ver cuotas de tarjeta que están por terminar (últimas 1-2 cuotas).",
        "input_schema": {
            "type": "object",
            "properties": {
                "remaining": {
                    "type": "integer",
                    "description": "Cuotas restantes máximas para considerar 'por terminar'. Default 2.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "add_loan",
        "description": (
            "Registrar un préstamo o devolución con un tercero (NO afecta gastos ni ingresos). "
            "'le presté X a Y' → prestamo_dado, 'Y me prestó X' → prestamo_recibido, "
            "'Y me devolvió X' → devolucion_recibida, 'le devolví X a Y' → devolucion_dada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contraparte": {"type": "string", "description": "Nombre de la persona"},
                "monto": {"type": "number"},
                "moneda": {
                    "type": "string",
                    "enum": ["ARS", "USD"],
                    "description": "Default ARS.",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["prestamo_dado", "prestamo_recibido", "devolucion_recibida", "devolucion_dada"],
                },
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD. Omitir para hoy."},
                "notas": {"type": "string"},
            },
            "required": ["contraparte", "monto", "moneda", "tipo"],
        },
    },
    {
        "name": "add_saving",
        "description": (
            "Registrar un ahorro. Guarda en la pestaña Ahorros y agrega un gasto 'Deposito Ahorro' "
            "que resta del saldo mensual. Usar para 'quiero ahorrar X', 'deposité X de ahorro'. "
            "En USD, convierte a ARS con el dólar blue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": {"type": "number"},
                "tipo": {
                    "type": "string",
                    "enum": ["Jubilacion", "Inversion Corto Plazo", "Ahorro Fisico", "Ahorro Virtual", "Crypto"],
                    "description": "Preguntar al usuario si no lo menciona.",
                },
                "moneda": {
                    "type": "string",
                    "enum": ["ARS", "USD"],
                    "description": "Default ARS.",
                },
                "notas": {"type": "string"},
                "fecha": {"type": "string"},
            },
            "required": ["monto", "tipo"],
        },
    },
    {
        "name": "get_loans_balance",
        "description": "Ver cuánto te deben y cuánto le debés a cada persona, en ARS y USD.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contraparte": {"type": "string", "description": "Filtrar por persona. Omitir para todos."},
            },
            "required": [],
        },
    },
]


def _sanitize(s: str, max_len: int = 500) -> str:
    if not isinstance(s, str):
        s = str(s)
    s = "".join(c for c in s if c >= " " or c == "\n")
    return s[:max_len].strip()


def execute_tool(name: str, tool_input: dict, user_config: dict,
                 pdf_path: str | None = None) -> str:
    """
    Ejecuta una tool y retorna el resultado como JSON string.
    user_config: dict del usuario (nombre, personas, tarjetas, sheet_id, etc.)
    """
    # Sanitizar strings de usuario
    for field in ("descripcion", "notas", "contraparte", "nombre"):
        if field in tool_input and isinstance(tool_input[field], str):
            tool_input[field] = _sanitize(tool_input[field])

    # Persona activa: primera persona de la lista del usuario
    personas = user_config.get("personas", [user_config.get("nombre", "Usuario")])
    persona_activa = tool_input.pop("persona", None) or (personas[0] if personas else "Usuario")

    # Validar que la persona solicitada existe en la planilla
    if persona_activa not in personas:
        persona_activa = personas[0] if personas else "Usuario"

    try:
        if name == "add_expense":
            result = tools_finance.add_expense(persona=persona_activa, **tool_input)
        elif name == "add_income":
            result = tools_finance.add_income(persona=persona_activa, **tool_input)
        elif name == "get_expenses":
            result = tools_finance.get_expenses(**tool_input)
        elif name == "delete_expense":
            result = tools_finance.delete_expense(persona=persona_activa, **tool_input)
        elif name == "get_monthly_summary":
            result = tools_dashboard.get_monthly_summary(**tool_input)
        elif name == "get_credit_card_debt":
            result = tools_dashboard.get_credit_card_debt(**tool_input)
        elif name == "parse_credit_card_pdf":
            if not pdf_path:
                result = {"ok": False, "error": "No se recibió ningún PDF. Enviá el archivo primero."}
            else:
                result = tools_pdf.parse_credit_card_pdf(
                    pdf_path=pdf_path, persona=persona_activa, **tool_input
                )
        elif name == "get_dolar_blue":
            result = tools_dashboard.get_dolar_blue()
        elif name == "pay_fixed_expense":
            result = tools_fixed.pay_fixed_expense(persona=persona_activa, **tool_input)
        elif name == "get_fixed_expenses_status":
            result = tools_fixed.get_fixed_expenses_status(**tool_input)
        elif name == "add_fixed_expense":
            result = tools_fixed.add_fixed_expense(persona=persona_activa, **tool_input)
        elif name == "get_next_month_projection":
            result = tools_dashboard.get_next_month_projection()
        elif name == "compare_months":
            result = tools_dashboard.compare_months(**tool_input)
        elif name == "get_installments_ending_soon":
            result = tools_dashboard.get_installments_ending_soon(**tool_input)
        elif name == "add_saving":
            result = tools_finance.add_saving(persona=persona_activa, **tool_input)
        elif name == "add_loan":
            result = tools_loans.add_loan(persona=persona_activa, **tool_input)
        elif name == "get_loans_balance":
            result = tools_loans.get_loans_balance(**tool_input)
        else:
            result = {"ok": False, "error": f"Tool desconocida: {name}"}
    except Exception as e:
        logger.exception("tool %s falló con input=%s", name, tool_input)
        result = {"ok": False, "error": str(e)}

    logger.info(
        "tool_exec name=%s input=%s result=%s",
        name,
        json.dumps(tool_input, ensure_ascii=False, default=str)[:300],
        json.dumps(result, ensure_ascii=False, default=str)[:300],
    )
    return json.dumps(result, ensure_ascii=False, default=str)
