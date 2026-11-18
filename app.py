from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
import sqlite3
import os
import hashlib
import hmac
import secrets
import csv
import io
import math
from datetime import datetime, timedelta
import asyncio
import qrcode


# ============================================================
# CONFIGURAÃ‡ÃƒO
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "loja.db")

_DOCS_ON = os.environ.get("LOJA_DOCS") == "1"

app = FastAPI(
    title="Restaurante SaaS",
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/master"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path == "/" or path.startswith("/static/"):
        # HTML/CSS/JS do painel: o navegador deve revalidar (ETag) a cada abertura,
        # senao um celular continua exibindo o CSS antigo apos um novo deploy.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ============================================================
# TEMPO REAL / WEBSOCKET
# ============================================================

class _SessionProbe:
    """Permite reutilizar get_user() com um token já conhecido (sem Request)."""

    def __init__(self, token):
        self.cookies = {"session": token}


def _ws_session_company(token):
    """company_id da sessão SE ela ainda for válida (existe, usuário ativo, não expirada); senão None."""
    if not token:
        return None

    user = get_user(_SessionProbe(token))

    if not user or user.get("company_id") is None:
        return None

    return int(user["company_id"])


def _ws_session_user(token):
    """Resolve a WebSocket identity exclusively from its HTTP-only session cookie."""
    if not token:
        return None
    try:
        return get_user(_SessionProbe(token))
    except Exception:
        return None


class ConnectionManager:
    """
    Conexões em tempo real ISOLADAS POR EMPRESA e REVALIDADAS.
    - Cada conexão pertence a uma company_id vinda da sessão autenticada.
    - broadcast() só entrega para conexões da mesma empresa; mensagem sem
      company_id não é enviada a ninguém (falha fechada).
    - Antes de cada envio a sessão é revalidada: logout, logout forçado,
      bloqueio do usuário ou expiração fecham a conexão.
    """

    def __init__(self):
        self.connections = {}
        self.master_connections = {}

    async def connect(self, websocket: WebSocket, company_id: int, token: str):
        await websocket.accept()
        self.connections[websocket] = (int(company_id), token)

    async def connect_master(self, websocket: WebSocket, token: str):
        await websocket.accept()
        self.master_connections[websocket] = token

    def disconnect(self, websocket: WebSocket):
        self.connections.pop(websocket, None)
        self.master_connections.pop(websocket, None)

    async def _drop(self, websocket):
        self.connections.pop(websocket, None)
        self.master_connections.pop(websocket, None)

        try:
            await websocket.close(code=1008)
        except Exception:
            pass

    async def broadcast(self, message: dict, company_id=None):
        target = company_id if company_id is not None else message.get("company_id")

        if target is None:
            return

        target = int(target)

        for websocket, (owner, token) in list(self.connections.items()):
            if owner != target:
                continue

            if _ws_session_company(token) != owner:
                await self._drop(websocket)
                continue

            try:
                await websocket.send_json(message)
            except Exception:
                self.connections.pop(websocket, None)

    async def broadcast_master(self, entity):
        # The MASTER channel carries only allow-listed invalidation notices;
        # it never carries tenant IDs, records, or client-supplied channel data.
        if entity not in {"platform_activity", "administration"}:
            return
        message = {"type": "admin_changed", "entity": entity}
        for websocket, token in list(self.master_connections.items()):
            user = _ws_session_user(token)
            if not user or user.get("role") != "master":
                await self._drop(websocket)
                continue
            try:
                await websocket.send_json(message)
            except Exception:
                self.master_connections.pop(websocket, None)


realtime_manager = ConnectionManager()
_REALTIME_LOOP = None


@app.on_event("startup")
async def capture_realtime_loop():
    global _REALTIME_LOOP
    _REALTIME_LOOP = asyncio.get_running_loop()


def notify_company_changed(company_id, entity="restaurant", details=None):
    loop = _REALTIME_LOOP
    if not loop or not loop.is_running() or company_id is None:
        return
    message = {"type": "data_changed", "entity": entity, "company_id": int(company_id)}
    if details:
        message.update(details)
    loop.call_soon_threadsafe(
        lambda: asyncio.create_task(
            realtime_manager.broadcast(message, company_id=int(company_id))
        )
    )
    notify_master_changed("platform_activity")


def notify_master_changed(entity="administration"):
    loop = _REALTIME_LOOP
    if entity not in {"platform_activity", "administration"}:
        return
    if not loop or not loop.is_running():
        return
    loop.call_soon_threadsafe(
        lambda: asyncio.create_task(realtime_manager.broadcast_master(entity))
    )


def _realtime_write_entity(path, method):
    """Strict route allow-list for post-success invalidation events."""
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if path.startswith(("/api/restaurant/", "/api/public/")):
        # These transactional routes already publish after commit, including
        # their request details; don't emit a second generic event.
        return None
    if path.startswith("/master/"):
        if "/api/" not in path:
            return None
        route = path.split("/api/", 1)[1].split("/", 1)[0]
        return route if route in {
            "companies", "users", "billing", "flag", "flags", "plan",
            "limits", "support", "sessions", "maintenance", "settings",
            "integrations", "backup", "api", "user", "company",
        } else "administration"
    if not path.startswith("/api/"):
        return None
    parts = path.strip("/").split("/")
    root = parts[1] if len(parts) > 1 else ""
    allowed = {
        "products": "product",
        "stock": "stock",
        "sales": "sale",
        "purchases": "purchase",
        "expenses": "expense",
        "customers": "customer",
        "suppliers": "supplier",
        "cash": "cash",
        "users": "user",
        "settings": "settings",
        "categories": "category",
    }
    return allowed.get(root)


@app.middleware("http")
async def realtime_after_successful_write(request: Request, call_next):
    response = await call_next(request)
    if response.status_code < 200 or response.status_code >= 300:
        return response
    entity = _realtime_write_entity(request.url.path, request.method.upper())
    if not entity:
        return response
    try:
        user = get_user(request)
        if request.url.path.startswith("/master/"):
            if user and user.get("role") == "master":
                notify_master_changed("administration")
        elif user and user.get("role") != "master" and user.get("company_id") is not None:
            # Write handlers return success only after their DB commit. Scope is
            # read from the server session, never from request parameters.
            notify_company_changed(int(user["company_id"]), entity)
    except Exception:
        # Realtime delivery is best-effort; it must not alter a committed API result.
        pass
    return response


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session")
    user = _ws_session_user(token)
    if not user:
        await websocket.close(code=1008)
        return
    role = user.get("role")
    company_id = user.get("company_id")
    if role == "master":
        await realtime_manager.connect_master(websocket, token)
    elif company_id is not None:
        company_id = int(company_id)
        await realtime_manager.connect(websocket, company_id, token)
    else:
        await websocket.close(code=1008)
        return

    try:
        while True:
            await websocket.receive_text()

            current = _ws_session_user(token)
            if not current or current.get("role") != role:
                await websocket.close(code=1008)
                break
            if role != "master" and current.get("company_id") != company_id:
                await websocket.close(code=1008)
                break

    except WebSocketDisconnect:
        pass

    except Exception:
        pass

    finally:
        realtime_manager.disconnect(websocket)


# ============================================================
# CONSTANTES
# ============================================================

ADMIN_ROLES = {"admin"}

MANAGEMENT_ROLES = {
    "admin",
    "gerente",
}

SELLER_ROLES = {
    "admin",
    "gerente",
    "vendedor",
    "caixa",
    "atendente",
}

OPERATIONAL_ROLES = {
    "admin",
    "gerente",
    "vendedor",
    "caixa",
    "atendente",
    "cozinha",
}

VALID_ROLES = {
    "admin",
    "gerente",
    "vendedor",
    "caixa",
    "atendente",
    "cozinha",
    "master",
}

PAYMENTS = {
    "PIX",
    "DINHEIRO",
    "CRÃ‰DITO",
    "DÃ‰BITO",
    "TRANSFERÃŠNCIA",
    "BOLETO",
    "OUTRO",
}

PHYSICAL_PAYMENT = {
    "DINHEIRO",
}

CASH_TYPES = {
    "suprimento",
    "sangria",
    "entrada",
    "saida",
    "ajuste",
    "venda",
}

INTEGER_UNITS = {
    "UN",
    "UND",
    "UNIDADE",
    "PAR",
    "PARES",
    "CX",
    "CAIXA",
    "PCT",
    "PACOTE",
    "DZ",
    "DUZIA",
}


# ============================================================
# DATABASE
# ============================================================

def get_user_company_id(user):
    """
    Retorna a empresa do usuário autenticado.
    Impede que uma requisição trabalhe fora da empresa da sessão.
    """
    company_id = user.get("company_id")
    if company_id is None:
        raise HTTPException(status_code=403, detail="Usuário sem empresa vinculada")
    return int(company_id)


def conn():
    c = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    c.row_factory = sqlite3.Row

    c.execute(
        "PRAGMA foreign_keys = ON"
    )

    c.execute(
        "PRAGMA busy_timeout = 5000"
    )

    return c


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def clean_text(value, max_length=None):
    if value is None:
        return None

    value = str(value).strip()

    if max_length:
        value = value[:max_length]

    return value


def normalize_username(value):
    value = clean_text(value, 80)

    if not value:
        return ""

    return value.lower()


def money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def is_finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validate_non_negative(value, message):
    if not is_finite(value) or float(value) < 0:
        raise HTTPException(
            400,
            message,
        )


def validate_positive(value, message):
    if not is_finite(value) or float(value) <= 0:
        raise HTTPException(
            400,
            message,
        )


def normalize_unit(value):
    unit = clean_text(value, 10)

    if not unit:
        return "UN"

    return unit.upper()


def validate_quantity(
    qty,
    unit="UN",
    allow_zero=False,
):
    if not is_finite(qty):
        raise HTTPException(
            400,
            "Quantidade invÃ¡lida",
        )

    qty = float(qty)

    if allow_zero:
        if qty < 0:
            raise HTTPException(
                400,
                "Quantidade nÃ£o pode ser negativa",
            )
    else:
        if qty <= 0:
            raise HTTPException(
                400,
                "Quantidade deve ser maior que zero",
            )

    normalized = normalize_unit(unit)

    if normalized in INTEGER_UNITS:
        if abs(qty - round(qty)) > 0.000001:
            raise HTTPException(
                400,
                f"A unidade {normalized} aceita somente quantidades inteiras",
            )

    return qty


def clean_barcode(value):
    value = clean_text(value, 80)

    if not value:
        return None

    value = "".join(
        ch
        for ch in value
        if ch.isalnum() or ch in "-_."
    )

    return value or None


# ============================================================
# DATABASE INIT
# ============================================================

def init_db():
    c = conn()

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'caixa',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE,
            barcode TEXT UNIQUE,
            name TEXT NOT NULL,
            category_id INTEGER,
            cost REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            stock REAL NOT NULL DEFAULT 0,
            min_stock REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'UN',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(category_id)
                REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cpf_cnpj TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suppliers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cpf_cnpj TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cash_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            opening_amount REAL NOT NULL DEFAULT 0,
            closed_at TEXT,
            closing_amount REAL,
            status TEXT NOT NULL DEFAULT 'open',
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sales(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            user_id INTEGER NOT NULL,
            cash_session_id INTEGER,
            subtotal REAL NOT NULL,
            discount REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL,
            payment_method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id)
                REFERENCES customers(id),
            FOREIGN KEY(user_id)
                REFERENCES users(id),
            FOREIGN KEY(cash_session_id)
                REFERENCES cash_sessions(id)
        );

        CREATE TABLE IF NOT EXISTS sale_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty REAL NOT NULL,
            unit_price REAL NOT NULL,
            total REAL NOT NULL,
            cost REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(sale_id)
                REFERENCES sales(id)
                ON DELETE CASCADE,
            FOREIGN KEY(product_id)
                REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER,
            user_id INTEGER NOT NULL,
            total REAL NOT NULL DEFAULT 0,
            invoice_ref TEXT,
            payment_method TEXT NOT NULL DEFAULT 'PIX',
            created_at TEXT NOT NULL,
            FOREIGN KEY(supplier_id)
                REFERENCES suppliers(id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS purchase_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty REAL NOT NULL,
            unit_cost REAL NOT NULL,
            total REAL NOT NULL,
            FOREIGN KEY(purchase_id)
                REFERENCES purchases(id)
                ON DELETE CASCADE,
            FOREIGN KEY(product_id)
                REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS stock_movements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            qty REAL NOT NULL,
            reason TEXT,
            reference TEXT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(product_id)
                REFERENCES products(id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS cash_movements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cash_session_id INTEGER,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            payment_method TEXT,
            description TEXT,
            reference TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(cash_session_id)
                REFERENCES cash_sessions(id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            category TEXT,
            amount REAL NOT NULL,
            payment_method TEXT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            entity TEXT,
            entity_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );
        """
    )

    # Multiempresa: bancos novos precisam da tabela companies e da coluna
    # company_id nas tabelas da loja. Em bancos existentes nada muda.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS companies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            document TEXT,
            phone TEXT,
            address TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            segment TEXT NOT NULL DEFAULT 'LOJA' CHECK(segment IN ('LOJA','RESTAURANTE'))
        )
        """
    )
    for tenant_table in (
        "users", "categories", "products", "customers", "suppliers",
        "cash_sessions", "sales", "purchases", "stock_movements",
        "expenses", "audit_log",
    ):
        tenant_cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tenant_table})")}
        if "company_id" not in tenant_cols:
            c.execute(f"ALTER TABLE {tenant_table} ADD COLUMN company_id INTEGER")
    settings_cols = {r["name"] for r in c.execute("PRAGMA table_info(settings)")}
    if "company_id" not in settings_cols and not c.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
        c.executescript(
            """
            DROP TABLE settings;
            CREATE TABLE settings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT,
                company_id INTEGER NOT NULL,
                UNIQUE(company_id, key)
            );
            """
        )

    # Migrações aditivas para o produto restaurante. Mantêm os dados atuais.
    product_cols = {r["name"] for r in c.execute("PRAGMA table_info(products)")}
    if "description" not in product_cols:
        c.execute("ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "category" not in product_cols:
        c.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''")

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS restaurant_tables(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            public_token TEXT UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(company_id, name)
        );
        CREATE TABLE IF NOT EXISTS restaurant_categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(company_id,name)
        );
        CREATE TABLE IF NOT EXISTS tabs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            table_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            customer_id INTEGER,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            sale_id INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(table_id) REFERENCES restaurant_tables(id),
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(sale_id) REFERENCES sales(id)
        );
        CREATE TABLE IF NOT EXISTS tab_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            tab_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            qty REAL NOT NULL,
            unit_price REAL NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            added_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(tab_id) REFERENCES tabs(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id),
            FOREIGN KEY(added_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS restaurant_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            tab_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'NOVO',
            notes TEXT NOT NULL DEFAULT '',
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'ATENDENTE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(tab_id) REFERENCES tabs(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS restaurant_order_items(
            order_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            PRIMARY KEY(order_id, item_id),
            FOREIGN KEY(order_id) REFERENCES restaurant_orders(id) ON DELETE CASCADE,
            FOREIGN KEY(item_id) REFERENCES tab_items(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS restaurant_order_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES restaurant_orders(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_restaurant_tables_company ON restaurant_tables(company_id, active);
        CREATE INDEX IF NOT EXISTS idx_restaurant_categories_company ON restaurant_categories(company_id, active);
        CREATE INDEX IF NOT EXISTS idx_tabs_company_status ON tabs(company_id, status);
        CREATE INDEX IF NOT EXISTS idx_tab_items_company_tab ON tab_items(company_id, tab_id);
        CREATE INDEX IF NOT EXISTS idx_restaurant_orders_company_status ON restaurant_orders(company_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_restaurant_order_history_company ON restaurant_order_history(company_id, order_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tabs_one_active_per_table
            ON tabs(table_id) WHERE status IN ('open','paying');
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tabs_company_number
            ON tabs(company_id,number);
        """
    )
    table_cols = {r["name"] for r in c.execute("PRAGMA table_info(restaurant_tables)")}
    if "public_token" not in table_cols:
        c.execute("ALTER TABLE restaurant_tables ADD COLUMN public_token TEXT")
    order_cols = {r["name"] for r in c.execute("PRAGMA table_info(restaurant_orders)")}
    if "source" not in order_cols:
        c.execute("ALTER TABLE restaurant_orders ADD COLUMN source TEXT NOT NULL DEFAULT 'ATENDENTE'")
    if "image_url" not in product_cols:
        c.execute("ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
    c.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_tables_public_token
            ON restaurant_tables(public_token) WHERE public_token IS NOT NULL;
        CREATE TABLE IF NOT EXISTS restaurant_table_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            table_id INTEGER NOT NULL,
            tab_id INTEGER,
            kind TEXT NOT NULL CHECK(kind IN ('WAITER','BILL')),
            status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','RESOLVED')),
            client_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by INTEGER,
            FOREIGN KEY(table_id) REFERENCES restaurant_tables(id),
            FOREIGN KEY(tab_id) REFERENCES tabs(id),
            FOREIGN KEY(resolved_by) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_restaurant_table_requests_open
            ON restaurant_table_requests(company_id,status,created_at);
        CREATE TABLE IF NOT EXISTS restaurant_public_submissions(
            company_id INTEGER NOT NULL,
            table_id INTEGER NOT NULL,
            client_key TEXT NOT NULL,
            request_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(company_id,table_id,client_key,request_id),
            FOREIGN KEY(order_id) REFERENCES restaurant_orders(id)
        );
        CREATE TABLE IF NOT EXISTS restaurant_public_request_limits(
            company_id INTEGER NOT NULL,
            table_id INTEGER NOT NULL,
            client_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_restaurant_public_limits_lookup
            ON restaurant_public_request_limits(company_id,table_id,client_key,kind,created_at);
    """)
    c.execute("""UPDATE products SET category=COALESCE((SELECT ca.name FROM categories ca
        WHERE ca.id=products.category_id AND ca.company_id=products.company_id), category, '')
        WHERE category IS NULL OR category=''""")
    c.execute("""INSERT OR IGNORE INTO restaurant_categories(company_id,name,created_at)
        SELECT DISTINCT company_id,name,? FROM categories WHERE company_id IS NOT NULL AND trim(name)<>''""", (now(),))

    # ========================================================
    # MIGRAÃ‡Ã•ES
    # ========================================================

    sale_cols = {
        r["name"]
        for r in c.execute(
            "PRAGMA table_info(sale_items)"
        ).fetchall()
    }

    if "cost" not in sale_cols:
        c.execute(
            """
            ALTER TABLE sale_items
            ADD COLUMN cost REAL NOT NULL DEFAULT 0
            """
        )

        c.execute(
            """
            UPDATE sale_items
            SET cost = COALESCE(
                (
                    SELECT p.cost
                    FROM products p
                    WHERE p.id = sale_items.product_id
                ),
                0
            )
            WHERE cost = 0
            """
        )

    cash_cols = {
        r["name"]
        for r in c.execute(
            "PRAGMA table_info(cash_movements)"
        ).fetchall()
    }

    if "payment_method" not in cash_cols:
        c.execute(
            """
            ALTER TABLE cash_movements
            ADD COLUMN payment_method TEXT
            """
        )

        c.execute(
            """
            UPDATE cash_movements
            SET payment_method = (
                SELECT s.payment_method
                FROM sales s
                WHERE 'VENDA-' || s.id =
                      cash_movements.reference
            )
            WHERE type='venda'
            """
        )

        c.execute(
            """
            UPDATE cash_movements
            SET payment_method = (
                SELECT e.payment_method
                FROM expenses e
                WHERE 'DESP-' || e.id =
                      cash_movements.reference
            )
            WHERE type='saida'
            AND reference LIKE 'DESP-%'
            """
        )

    purchase_cols = {
        r["name"]
        for r in c.execute(
            "PRAGMA table_info(purchases)"
        ).fetchall()
    }

    if "payment_method" not in purchase_cols:
        c.execute(
            """
            ALTER TABLE purchases
            ADD COLUMN payment_method TEXT
            NOT NULL DEFAULT 'PIX'
            """
        )

    # ========================================================
    # ÃNDICES
    # ========================================================

    indexes = [
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_barcode
        ON products(barcode)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_sku
        ON products(sku)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_active
        ON products(active)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_name
        ON products(name)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sales_user
        ON sales(user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sales_status
        ON sales(status)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sales_created
        ON sales(created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sale_items_sale
        ON sale_items(sale_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sale_items_product
        ON sale_items(product_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_stock_movements_product
        ON stock_movements(product_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_stock_movements_user
        ON stock_movements(user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_cash_movements_session
        ON cash_movements(cash_session_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_cash_movements_user
        ON cash_movements(user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_sessions_user
        ON sessions(user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
        idx_audit_user
        ON audit_log(user_id)
        """,
    ]

    for index_sql in indexes:
        c.execute(index_sql)

    # ========================================================
    # ADMIN INICIAL
    # ========================================================

    if not c.execute(
        "SELECT 1 FROM users LIMIT 1"
    ).fetchone():

        initial_password = os.environ.get("LOJA_INITIAL_ADMIN_PASSWORD", "")
        if len(initial_password) < 12:
            c.close()
            raise RuntimeError(
                "Banco sem usuários. Defina LOJA_INITIAL_ADMIN_PASSWORD "
                "com pelo menos 12 caracteres para criar o acesso inicial."
            )

        salt, hp = hash_password(initial_password)

        company_id = c.execute(
            "INSERT INTO companies(name, active, created_at) VALUES(?,?,?)",
            ("Minha Empresa", 1, now()),
        ).lastrowid

        c.execute(
            """
            INSERT INTO users(
                name,
                username,
                password_salt,
                password_hash,
                role,
                active,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "Administrador",
                "admin",
                salt,
                hp,
                "admin",
                1,
                now(),
                company_id,
            ),
        )

    defaults = {
        "store_name": "Meu Restaurante",
        "store_document": "",
        "store_phone": "",
        "store_address": "",
        "currency": "BRL",
    }

    for key, value in defaults.items():
        c.execute(
            """
            INSERT OR IGNORE INTO settings(
                key,
                value
            )
            VALUES(?,?)
            """,
            (key, value),
        )

    c.commit()
    c.close()


# ============================================================
# MASTER GLOBAL
# ============================================================

def require_master(request: Request):
    user = require_user(request)

    if user["role"] != "master":
        raise HTTPException(
            status_code=403,
            detail="Acesso exclusivo do MASTER",
        )

    return user


def forbid_master_role_assignment(role):
    """
    O perfil 'master' só pode ser atribuído por um usuário autenticado
    via require_master (endpoints /api/master/*). Os endpoints de
    usuários de empresa (/api/users) nunca podem criar nem promover
    para este perfil.
    """
    if str(role or "").strip().lower() == "master":
        raise HTTPException(
            status_code=403,
            detail="Apenas o MASTER pode atribuir o perfil master",
        )



# ============================================================
# PASSWORDS
# ============================================================

def hash_password(
    password: str,
    salt: Optional[str] = None,
):
    salt = salt or secrets.token_hex(16)

    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        150_000,
    )

    return salt, dk.hex()


def verify_password(
    password,
    salt,
    stored,
):
    _, candidate = hash_password(
        password,
        salt,
    )

    return hmac.compare_digest(
        candidate,
        stored,
    )


# Inicializa banco
init_db()


# ============================================================
# AUTH
# ============================================================

SESSION_MAX_DAYS = int(os.environ.get("LOJA_SESSION_DAYS", "7"))
MASTER_SESSION_MAX_HOURS = int(os.environ.get("LOJA_MASTER_SESSION_HOURS", "24"))


def session_expired(created_at, role):
    """Sessão vence no servidor (o cookie sozinho não basta): 7 dias; MASTER 24h."""
    try:
        started = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    limit = (
        timedelta(hours=MASTER_SESSION_MAX_HOURS)
        if str(role).lower() == "master"
        else timedelta(days=SESSION_MAX_DAYS)
    )
    return datetime.now() - started > limit


def get_user(request: Request):
    token = request.cookies.get("session")

    if not token:
        return None

    c = conn()

    row = c.execute(
        """
        SELECT
            u.id,
            u.name,
            u.username,
            u.role,
            u.active,
            u.company_id,
            co.segment AS segment,
            co.active AS company_active,
            s.created_at
        FROM sessions s
        JOIN users u
            ON u.id=s.user_id
        LEFT JOIN companies co
            ON co.id=u.company_id
        WHERE s.token=?
        """,
        (token,),
    ).fetchone()

    company_active = True
    if row and row["role"] != "master" and row["company_id"] is not None:
        company_active = bool(row["company_active"])

    c.close()

    if not row or not row["active"] or not company_active:
        return None

    if row["role"] not in VALID_ROLES:
        return None

    if session_expired(row["created_at"], row["role"]):
        return None

    result = dict(row)
    result.pop("created_at", None)
    result.pop("company_active", None)
    if result.get("company_id") is not None:
        result["segment"] = result.get("segment") or "LOJA"

    return result


def require_user(request: Request):
    user = get_user(request)

    if not user:
        raise HTTPException(
            401,
            "NÃ£o autenticado",
        )

    return user


def require_role(request: Request, roles):
    user = require_user(request)

    if user["role"] not in roles:
        raise HTTPException(
            403,
            "VocÃª nÃ£o possui permissÃ£o para esta operaÃ§Ã£o",
        )

    return user


def audit(
    user_id,
    action,
    entity=None,
    entity_id=None,
    details=None,
    cursor=None,
):
    own = cursor is None

    c = cursor or conn()

    company_id = None

    if user_id is not None:
        row = c.execute(
            """
            SELECT company_id
            FROM users
            WHERE id=?
            """,
            (user_id,),
        ).fetchone()

        if row:
            company_id = row["company_id"]

    c.execute(
        """
        INSERT INTO audit_log(
            user_id,
            action,
            entity,
            entity_id,
            details,
            company_id,
            created_at
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            user_id,
            action,
            entity,
            entity_id,
            details,
            company_id,
            now(),
        ),
    )

    if own:
        c.commit()
        c.close()


# ============================================================
# MODELS
# ============================================================

class LoginIn(BaseModel):
    username: str
    password: str


class ProductIn(BaseModel):
    sku: Optional[str] = None
    barcode: Optional[str] = None
    name: str
    description: Optional[str] = ""
    category: Optional[str] = None
    cost: float = 0
    price: float = 0
    stock: float = 0
    min_stock: float = 0
    unit: str = "UN"
    active: bool = True


class ProductStatusIn(BaseModel):
    active: bool


class ProductBulkDeleteIn(BaseModel):
    ids: List[int]


class PartyIn(BaseModel):
    name: str
    cpf_cnpj: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


class SaleItemIn(BaseModel):
    product_id: int
    qty: float
    unit_price: Optional[float] = None


class SaleIn(BaseModel):
    customer_id: Optional[int] = None
    items: List[SaleItemIn]
    discount: float = 0
    payment_method: str = "PIX"


class RestaurantTableIn(BaseModel):
    name: str


class RestaurantCategoryIn(BaseModel):
    name: str


class TabOpenIn(BaseModel):
    customer_id: Optional[int] = None


class TabItemIn(BaseModel):
    product_id: int
    qty: float = 1
    notes: Optional[str] = ""


class TabItemUpdateIn(BaseModel):
    qty: float
    notes: Optional[str] = ""


class TabTransferIn(BaseModel):
    table_id: int


class TabNotesIn(BaseModel):
    notes: Optional[str] = ""


class TabCloseIn(BaseModel):
    payment_method: str
    customer_id: Optional[int] = None


class OrderStatusIn(BaseModel):
    status: str


class RestaurantQrIn(BaseModel):
    regenerate: bool = False


class PublicOrderItemIn(BaseModel):
    product_id: int = Field(gt=0)
    qty: float = Field(gt=0, le=99)
    notes: Optional[str] = ""


class PublicOrderIn(BaseModel):
    client_id: str = Field(min_length=20, max_length=100)
    request_id: str = Field(min_length=12, max_length=100)
    items: List[PublicOrderItemIn] = Field(min_length=1, max_length=50)


class PublicTableRequestIn(BaseModel):
    client_id: str = Field(min_length=20, max_length=100)
    kind: str


class PurchaseItemIn(BaseModel):
    product_id: int
    qty: float
    unit_cost: float


class PurchaseIn(BaseModel):
    supplier_id: Optional[int] = None
    invoice_ref: Optional[str] = None
    payment_method: str = "PIX"
    items: List[PurchaseItemIn]


class ExpenseIn(BaseModel):
    description: str
    category: Optional[str] = None
    amount: float
    payment_method: Optional[str] = None


class CashOpenIn(BaseModel):
    opening_amount: float = 0


class CashCloseIn(BaseModel):
    closing_amount: float


class CashMoveIn(BaseModel):
    type: str
    amount: float
    description: Optional[str] = None


class StockAdjustIn(BaseModel):
    product_id: int
    qty: float
    reason: str = "Ajuste manual"


class StockSetIn(BaseModel):
    product_id: int
    stock: float
    reason: str = "Estoque definido manualmente"


class UserIn(BaseModel):
    name: str
    username: str
    password: str
    role: str = "caixa"
    active: bool = True


class UserUpdateIn(BaseModel):
    name: str
    username: str
    role: str
    active: bool = True
    password: Optional[str] = None


class SettingsIn(BaseModel):
    store_name: str
    store_document: Optional[str] = ""
    store_phone: Optional[str] = ""
    store_address: Optional[str] = ""


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


# ============================================================
# PAGES
# ============================================================

@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(
        os.path.join(
            BASE_DIR,
            "static",
            "index.html",
        )
    )




# ============================================================
# AUTH API
# ============================================================

import threading as _threading
import time as _time

LOGIN_MAX_FAILS = int(os.environ.get("LOJA_LOGIN_MAX_FAILS", "8"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOJA_LOGIN_WINDOW_SECONDS", "600"))
_LOGIN_FAILS = {}
_LOGIN_LOCK = _threading.Lock()


def _login_key(request: Request, username: str):
    host = request.client.host if request.client else "?"
    return (host, username)


def _login_blocked(key):
    now_ts = _time.time()
    with _LOGIN_LOCK:
        hits = [x for x in _LOGIN_FAILS.get(key, []) if now_ts - x < LOGIN_WINDOW_SECONDS]
        if hits:
            _LOGIN_FAILS[key] = hits
        else:
            _LOGIN_FAILS.pop(key, None)
        return len(hits) >= LOGIN_MAX_FAILS


def _login_fail(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(key, []).append(_time.time())
        if len(_LOGIN_FAILS) > 5000:
            _LOGIN_FAILS.clear()


def _login_ok(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(key, None)


def _is_https(request: Request):
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


@app.post("/api/login")
def login(
    data: LoginIn,
    response: Response,
    request: Request,
):
    username = normalize_username(
        data.username
    )

    _key = _login_key(request, username)

    if _login_blocked(_key):
        raise HTTPException(
            429,
            "Muitas tentativas de login. Aguarde alguns minutos e tente novamente.",
        )

    if not username:
        raise HTTPException(
            400,
            "Informe o usuÃ¡rio",
        )

    c = conn()

    row = c.execute(
        """
        SELECT *
        FROM users
        WHERE username=?
        AND active=1
        """,
        (username,),
    ).fetchone()

    if not row or not verify_password(
        data.password,
        row["password_salt"],
        row["password_hash"],
    ):
        c.close()
        _login_fail(_key)

        raise HTTPException(
            401,
            "Senha ou usuário incorreto",
        )

    company_segment = None
    if row["role"] != "master" and row["company_id"] is not None:
        try:
            company = c.execute(
                "SELECT active,segment FROM companies WHERE id=?",
                (row["company_id"],),
            ).fetchone()
        except sqlite3.OperationalError:
            company = None
        if not company or not company["active"]:
            c.close()
            raise HTTPException(403,"Empresa suspensa ou indisponível")

    if row["role"] != "master" and row["company_id"] is not None and company:
        company_segment = company["segment"] or "LOJA"

    _login_ok(_key)

    token = secrets.token_urlsafe(32)

    c.execute(
        "DELETE FROM sessions WHERE created_at < ?",
        ((datetime.now() - timedelta(days=SESSION_MAX_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),),
    )

    c.execute(
        """
        INSERT INTO sessions(
            token,
            user_id,
            created_at
        )
        VALUES(?,?,?)
        """,
        (
            token,
            row["id"],
            now(),
        ),
    )

    c.commit()
    c.close()

    response.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        max_age=86400 * 7,
    )

    return {
        "ok": True,
        "user": {
            "id": row["id"],
            "name": row["name"],
            "username": row["username"],
            "role": row["role"],
            "company_id": row["company_id"],
            "segment": company_segment,
        },
    }


@app.post("/api/logout")
def logout(
    request: Request,
    response: Response,
):
    token = request.cookies.get(
        "session"
    )

    if token:
        c = conn()

        c.execute(
            """
            DELETE FROM sessions
            WHERE token=?
            """,
            (token,),
        )

        c.commit()
        c.close()

    response.delete_cookie(
        "session"
    )

    return {
        "ok": True
    }


@app.get("/api/me")
def me(request: Request):
    return require_user(request)


@app.post("/api/change-password")
def change_password(
    data: ChangePasswordIn,
    request: Request,
):
    user = require_user(request)

    if len(data.new_password) < 6:
        raise HTTPException(
            400,
            "A nova senha deve ter pelo menos 6 caracteres",
        )

    c = conn()

    row = c.execute(
        """
        SELECT *
        FROM users
        WHERE id=?
        """,
        (user["id"],),
    ).fetchone()

    if not row:
        c.close()

        raise HTTPException(
            404,
            "UsuÃ¡rio nÃ£o encontrado",
        )

    if not verify_password(
        data.current_password,
        row["password_salt"],
        row["password_hash"],
    ):
        c.close()

        raise HTTPException(
            400,
            "Senha atual incorreta",
        )

    salt, hp = hash_password(
        data.new_password
    )

    c.execute(
        """
        UPDATE users
        SET
            password_salt=?,
            password_hash=?
        WHERE id=?
        """,
        (
            salt,
            hp,
            user["id"],
        ),
    )

    audit(
        user["id"],
        "change_password",
        "user",
        user["id"],
        cursor=c,
    )

    # Encerra sessÃµes antigas.
    c.execute(
        """
        DELETE FROM sessions
        WHERE user_id=?
        """,
        (user["id"],),
    )

    c.commit()
    c.close()

    return {
        "ok": True
    }


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/api/dashboard")
def dashboard(request: Request):
    user = require_user(request)

    c = conn()

    today = datetime.now().strftime("%Y-%m-%d")

    if user["role"] in MANAGEMENT_ROLES:
        sales_where = "AND s.company_id=?"
        sales_params = (
            user["company_id"],
        )

        expense_where = "AND e.company_id=?"
        expense_params = (
            user["company_id"],
        )

    else:
        sales_where = "AND s.user_id=? AND s.company_id=?"
        sales_params = (
            user["id"],
            user["company_id"],
        )

        expense_where = "AND e.user_id=? AND e.company_id=?"
        expense_params = (
            user["id"],
            user["company_id"],
        )

    sales_today = c.execute(
        f"""
        SELECT
            COALESCE(
                SUM(s.total),
                0
            ) value
        FROM sales s
        WHERE s.status='completed'
        AND date(s.created_at)=?
        {sales_where}
        """,
        (today,) + sales_params,
    ).fetchone()["value"]

    sales_count_today = c.execute(
        f"""
        SELECT
            COUNT(*) value
        FROM sales s
        WHERE s.status='completed'
        AND date(s.created_at)=?
        {sales_where}
        """,
        (today,) + sales_params,
    ).fetchone()["value"]

    expenses_today = 0
    if user.get("segment") != "RESTAURANTE":
        expenses_today = c.execute(
            f"""
            SELECT COALESCE(SUM(e.amount),0) value
            FROM expenses e
            WHERE date(e.created_at)=?
            {expense_where}
            """,
            (today,) + expense_params,
        ).fetchone()["value"]

    stock_value = c.execute(
        """
        SELECT
            COALESCE(
                SUM(stock * cost),
                0
            ) value
        FROM products
        WHERE active=1
        AND company_id=?
        """,
        (user["company_id"],),
    ).fetchone()["value"]

    low_stock = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                id,
                name,
                stock,
                min_stock,
                unit
            FROM products
            WHERE active=1
            AND stock <= min_stock
            AND company_id=?
            ORDER BY stock ASC
            LIMIT 10
            """,
            (user["company_id"],),
        ).fetchall()
    ]

    if user["role"] in MANAGEMENT_ROLES:

        top_products = [
            dict(r)
            for r in c.execute(
                """
                SELECT
                    p.name,
                    SUM(si.qty) qty,
                    SUM(si.total) revenue
                FROM sale_items si
                JOIN sales s
                    ON s.id=si.sale_id
                JOIN products p
                    ON p.id=si.product_id
                WHERE s.status='completed'
                AND s.company_id=?
                AND p.company_id=?
                GROUP BY p.id
                ORDER BY qty DESC
                LIMIT 8
                """,
                (
                    user["company_id"],
                    user["company_id"],
                ),
            ).fetchall()
        ]

        recent_sales = [
            dict(r)
            for r in c.execute(
                """
                SELECT
                    s.id,
                    s.total,
                    s.payment_method,
                    s.created_at,
                    COALESCE(
                        cu.name,
                        'Consumidor'
                    ) customer,
                    u.name user_name
                FROM sales s
                LEFT JOIN customers cu
                    ON cu.id=s.customer_id
                    AND cu.company_id=?
                JOIN users u
                    ON u.id=s.user_id
                    AND u.company_id=?
                WHERE s.company_id=?
                ORDER BY s.id DESC
                LIMIT 8
                """,
                (
                    user["company_id"],
                    user["company_id"],
                    user["company_id"],
                ),
            ).fetchall()
        ]

    else:

        top_products = [
            dict(r)
            for r in c.execute(
                """
                SELECT
                    p.name,
                    SUM(si.qty) qty,
                    SUM(si.total) revenue
                FROM sale_items si
                JOIN sales s
                    ON s.id=si.sale_id
                JOIN products p
                    ON p.id=si.product_id
                WHERE s.status='completed'
                AND s.user_id=?
                AND s.company_id=?
                AND p.company_id=?
                GROUP BY p.id
                ORDER BY qty DESC
                LIMIT 8
                """,
                (
                    user["id"],
                    user["company_id"],
                    user["company_id"],
                ),
            ).fetchall()
        ]

        recent_sales = [
            dict(r)
            for r in c.execute(
                """
                SELECT
                    s.id,
                    s.total,
                    s.payment_method,
                    s.created_at,
                    COALESCE(
                        cu.name,
                        'Consumidor'
                    ) customer,
                    u.name user_name
                FROM sales s
                LEFT JOIN customers cu
                    ON cu.id=s.customer_id
                    AND cu.company_id=?
                JOIN users u
                    ON u.id=s.user_id
                    AND u.company_id=?
                WHERE s.user_id=?
                AND s.company_id=?
                ORDER BY s.id DESC
                LIMIT 8
                """,
                (
                    user["company_id"],
                    user["company_id"],
                    user["id"],
                    user["company_id"],
                ),
            ).fetchall()
        ]

    restaurant_scope = "s.company_id=?"
    restaurant_params = [user["company_id"]]
    if user["role"] not in MANAGEMENT_ROLES:
        restaurant_scope += " AND s.user_id=?"
        restaurant_params.append(user["id"])
    payment_methods = [dict(r) for r in c.execute(f"""
        SELECT s.payment_method,COUNT(*) count,COALESCE(SUM(s.total),0) total
        FROM sales s WHERE s.status='completed' AND date(s.created_at)=?
          AND {restaurant_scope} GROUP BY s.payment_method ORDER BY total DESC
    """, [today]+restaurant_params).fetchall()]
    open_tables = c.execute("""SELECT COUNT(DISTINCT table_id) FROM tabs
        WHERE company_id=? AND status IN ('open','paying')""", (user["company_id"],)).fetchone()[0]
    open_tabs = c.execute("SELECT COUNT(*) FROM tabs WHERE company_id=? AND status IN ('open','paying')", (user["company_id"],)).fetchone()[0]
    orders_today = c.execute("SELECT COUNT(*) FROM restaurant_orders WHERE company_id=? AND date(created_at)=?", (user["company_id"],today)).fetchone()[0]
    cash = current_cash(c, user["id"], user["company_id"])
    cash_balance = cash_expected(c, cash["id"]) if cash else None

    c.close()

    return {
        "sales_today": money(
            sales_today
        ),
        "sales_count_today":
            sales_count_today,
        "expenses_today": money(
            expenses_today
        ),
        "stock_value": money(
            stock_value
        ),
        "low_stock": low_stock,
        "top_products": top_products,
        "recent_sales": recent_sales,
        "role": user["role"],
        "orders_today": int(orders_today),
        "average_ticket": money(sales_today / sales_count_today) if sales_count_today else 0,
        "open_tables": int(open_tables),
        "open_tabs": int(open_tabs),
        "cash_balance": cash_balance,
        "payment_methods": payment_methods,
    }
@app.get("/api/products")
def products(
    request: Request,
    q: Optional[str] = None,
    include_inactive: bool = False,
):
    user = require_user(request)

    if include_inactive and user["role"] not in MANAGEMENT_ROLES:
        include_inactive = False

    c = conn()

    conditions = [
        "p.company_id=?"
    ]
    params = [
        user["company_id"]
    ]

    if not include_inactive:
        conditions.append(
            "p.active=1"
        )

    if q:
        search_text = clean_text(
            q,
            120,
        )

        if search_text:
            search = (
                f"%{search_text}%"
            )

            conditions.append(
                """
                (
                    p.name LIKE ?
                    OR p.sku LIKE ?
                    OR p.barcode LIKE ?
                )
                """
            )

            params.extend(
                [
                    search,
                    search,
                    search,
                ]
            )

    where = ""

    if conditions:
        where = (
            "WHERE "
            + " AND ".join(conditions)
        )

    rows = c.execute(
        f"""
        SELECT
            p.*,
            COALESCE(NULLIF(p.category,''),ca.name,'') category
        FROM products p
        LEFT JOIN categories ca
            ON ca.id=p.category_id AND ca.company_id=p.company_id
        {where}
        ORDER BY
            p.active DESC,
            p.name
        """,
        params,
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


@app.post("/api/products")
def create_product(
    data: ProductIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    name = clean_text(
        data.name,
        150,
    )

    if not name:
        raise HTTPException(
            400,
            "Nome do produto Ã© obrigatÃ³rio",
        )

    validate_non_negative(
        data.cost,
        "PreÃ§o de custo invÃ¡lido",
    )

    validate_non_negative(
        data.price,
        "PreÃ§o de venda invÃ¡lido",
    )

    validate_non_negative(
        data.stock,
        "Estoque inicial nÃ£o pode ser negativo",
    )

    validate_non_negative(
        data.min_stock,
        "Estoque mÃ­nimo nÃ£o pode ser negativo",
    )

    unit = normalize_unit(
        data.unit
    )

    stock = validate_quantity(
        data.stock,
        unit,
        allow_zero=True,
    )

    min_stock = validate_quantity(
        data.min_stock,
        unit,
        allow_zero=True,
    )

    sku = clean_text(
        data.sku,
        80,
    )

    barcode = clean_barcode(
        data.barcode
    )

    category_name = clean_text(
        data.category,
        100,
    )

    c = conn()

    if category_name and not c.execute("SELECT 1 FROM restaurant_categories WHERE company_id=? AND name=? AND active=1", (user["company_id"],category_name)).fetchone():
        c.close()
        raise HTTPException(400,"Categoria não encontrada nesta empresa")

    try:
        cur = c.execute(
            """
            INSERT INTO products(
                sku,
                barcode,
                name,
                description,
                category,
                category_id,
                cost,
                price,
                stock,
                min_stock,
                unit,
                active,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sku or None,
                barcode or None,
                name,
                clean_text(data.description, 1000) or "",
                category_name or "",
                None,
                money(data.cost),
                money(data.price),
                stock,
                min_stock,
                unit,
                1 if data.active else 0,
                now(),
                user["company_id"],
            ),
        )

        product_id = cur.lastrowid

        if stock > 0:
            c.execute(
                """
                INSERT INTO stock_movements(
                    product_id,
                    type,
                    qty,
                    reason,
                    reference,
                    user_id,
                    created_at,
                    company_id
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    product_id,
                    "entrada",
                    stock,
                    "Estoque inicial",
                    "CADASTRO",
                    user["id"],
                    now(),
                    user["company_id"],
                ),
            )

        audit(
            user["id"],
            "create",
            "product",
            product_id,
            name,
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "id": product_id,
        }

    except sqlite3.IntegrityError:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            "SKU ou cÃ³digo de barras jÃ¡ cadastrado",
        )


@app.put("/api/products/{pid}")
def update_product(
    pid: int,
    data: ProductIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    name = clean_text(
        data.name,
        150,
    )

    if not name:
        raise HTTPException(
            400,
            "Nome do produto Ã© obrigatÃ³rio",
        )

    validate_non_negative(
        data.cost,
        "PreÃ§o de custo invÃ¡lido",
    )

    validate_non_negative(
        data.price,
        "PreÃ§o de venda invÃ¡lido",
    )

    validate_non_negative(
        data.min_stock,
        "Estoque mÃ­nimo nÃ£o pode ser negativo",
    )

    unit = normalize_unit(
        data.unit
    )

    min_stock = validate_quantity(
        data.min_stock,
        unit,
        allow_zero=True,
    )

    c = conn()

    old = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            pid,
            user["company_id"],
        ),
    ).fetchone()

    if not old:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    category_name = clean_text(
        data.category,
        100,
    )

    if category_name and not c.execute("SELECT 1 FROM restaurant_categories WHERE company_id=? AND name=? AND active=1", (user["company_id"],category_name)).fetchone():
        c.close()
        raise HTTPException(400,"Categoria não encontrada nesta empresa")

    try:
        sku = clean_text(
            data.sku,
            80,
        )

        barcode = clean_barcode(
            data.barcode
        )

        # IMPORTANTE:
        # O estoque NÃƒO Ã© alterado aqui.
        # AlteraÃ§Ã£o de estoque deve passar
        # por /api/stock/adjust ou /api/stock/set.

        c.execute(
            """
            UPDATE products
            SET
                sku=?,
                barcode=?,
                name=?,
                description=?,
                category=?,
                category_id=NULL,
                cost=?,
                price=?,
                min_stock=?,
                unit=?,
                active=?
            WHERE id=?
              AND company_id=?
            """,
            (
                sku or None,
                barcode or None,
                name,
                clean_text(data.description, 1000) or "",
                category_name or "",
                money(data.cost),
                money(data.price),
                min_stock,
                unit,
                1 if data.active else 0,
                pid,
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "update",
            "product",
            pid,
            name,
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "stock": old["stock"],
        }

    except sqlite3.IntegrityError:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            "SKU ou cÃ³digo de barras jÃ¡ cadastrado",
        )


# ============================================================
# EXCLUSÃƒO / INATIVAÃ‡ÃƒO DE PRODUTOS
# ============================================================

@app.delete("/api/products/{pid}")
def delete_product(
    pid: int,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    c = conn()

    product = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            pid,
            user["company_id"],
        ),
    ).fetchone()

    if not product:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    sales_count = c.execute(
        """
        SELECT COUNT(*)
        FROM sale_items
        WHERE product_id=?
        """,
        (pid,),
    ).fetchone()[0]

    purchase_count = c.execute(
        """
        SELECT COUNT(*)
        FROM purchase_items
        WHERE product_id=?
        """,
        (pid,),
    ).fetchone()[0]

    movement_count = c.execute(
        """
        SELECT COUNT(*)
        FROM stock_movements
        WHERE product_id=?
        """,
        (pid,),
    ).fetchone()[0]

    has_history = (
        sales_count > 0
        or purchase_count > 0
        or movement_count > 0
    )

    if has_history:

        c.execute(
            """
            UPDATE products
            SET active=0
            WHERE id=?
              AND company_id=?
            """,
            (
                pid,
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "deactivate",
            "product",
            pid,
            (
                "Produto inativado; "
                f"vendas={sales_count}; "
                f"compras={purchase_count}; "
                f"movimentos={movement_count}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "action": "deactivated",
            "message":
                "O produto possui histÃ³rico e foi inativado para preservar os registros.",
        }

    c.execute(
        """
        DELETE FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            pid,
            user["company_id"],
        ),
    )

    audit(
        user["id"],
        "delete",
        "product",
        pid,
        product["name"],
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "action": "deleted",
        "message":
            "Produto excluÃ­do definitivamente.",
    }



@app.post("/api/products/bulk-delete")
def bulk_delete_products(
    data: ProductBulkDeleteIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    if not data.ids:
        raise HTTPException(
            400,
            "Nenhum produto foi selecionado",
        )

    ids = list(dict.fromkeys(data.ids))

    if any(int(pid) <= 0 for pid in ids):
        raise HTTPException(
            400,
            "ID de produto inv?lido",
        )

    c = conn()

    deleted = 0
    deactivated = 0
    not_found = 0

    try:
        c.execute("BEGIN")

        for pid in ids:
            product = c.execute(
                """
                SELECT *
                FROM products
                WHERE id=?
                  AND company_id=?
                """,
                (
                    pid,
                    user["company_id"],
                ),
            ).fetchone()

            if not product:
                not_found += 1
                continue

            sales_count = c.execute(
                """
                SELECT COUNT(*)
                FROM sale_items
                WHERE product_id=?
                """,
                (pid,),
            ).fetchone()[0]

            purchase_count = c.execute(
                """
                SELECT COUNT(*)
                FROM purchase_items
                WHERE product_id=?
                """,
                (pid,),
            ).fetchone()[0]

            movement_count = c.execute(
                """
                SELECT COUNT(*)
                FROM stock_movements
                WHERE product_id=?
                """,
                (pid,),
            ).fetchone()[0]

            has_history = (
                sales_count > 0
                or purchase_count > 0
                or movement_count > 0
            )

            if has_history:
                c.execute(
                    """
                    UPDATE products
                    SET active=0
                    WHERE id=?
                      AND company_id=?
                    """,
                    (
                        pid,
                        user["company_id"],
                    ),
                )

                audit(
                    user["id"],
                    "deactivate",
                    "product",
                    pid,
                    (
                        "Produto inativado em exclus?o m?ltipla; "
                        f"vendas={sales_count}; "
                        f"compras={purchase_count}; "
                        f"movimentos={movement_count}"
                    ),
                    c,
                )

                deactivated += 1

            else:
                c.execute(
                    """
                    DELETE FROM products
                    WHERE id=?
                      AND company_id=?
                    """,
                    (
                        pid,
                        user["company_id"],
                    ),
                )

                audit(
                    user["id"],
                    "delete",
                    "product",
                    pid,
                    product["name"],
                    c,
                )

                deleted += 1

        c.commit()
        c.close()

        return {
            "ok": True,
            "requested": len(ids),
            "deleted": deleted,
            "deactivated": deactivated,
            "not_found": not_found,
            "message": (
                f"{deleted} produto(s) exclu?do(s) definitivamente; "
                f"{deactivated} produto(s) inativado(s)."
            ),
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception as e:
        c.rollback()
        c.close()
        raise HTTPException(
            400,
            str(e),
        )


@app.patch("/api/products/{pid}/status")
def product_status(
    pid: int,
    data: ProductStatusIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    c = conn()

    product = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            pid,
            user["company_id"],
        ),
    ).fetchone()

    if not product:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    c.execute(
        """
        UPDATE products
        SET active=?
        WHERE id=?
          AND company_id=?
        """,
        (
            1 if data.active else 0,
            pid,
            user["company_id"],
        ),
    )

    audit(
        user["id"],
        "activate"
        if data.active
        else "deactivate",
        "product",
        pid,
        product["name"],
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "active": data.active,
    }


# ============================================================
# ESTOQUE
# ============================================================

@app.post("/api/stock/adjust")
def adjust_stock(
    data: StockAdjustIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    if not is_finite(data.qty):
        raise HTTPException(
            400,
            "Quantidade invÃ¡lida",
        )

    if data.qty == 0:
        raise HTTPException(
            400,
            "A quantidade do ajuste nÃ£o pode ser zero",
        )

    reason = clean_text(
        data.reason,
        200,
    ) or "Ajuste manual"

    c = conn()

    product = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            data.product_id,
            user["company_id"],
        ),
    ).fetchone()

    if not product:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    qty = validate_quantity(
        abs(data.qty),
        product["unit"],
    )

    signed_qty = (
        qty
        if data.qty > 0
        else -qty
    )

    new_stock = money(
        product["stock"] + signed_qty
    )

    if new_stock < 0:
        c.close()

        raise HTTPException(
            400,
            (
                "Estoque insuficiente. "
                f"Estoque atual: {product['stock']}"
            ),
        )

    try:
        c.execute("BEGIN")

        c.execute(
            """
            UPDATE products
            SET stock=?
            WHERE id=?
              AND company_id=?
            """,
            (
                new_stock,
                data.product_id,
                user["company_id"],
            ),
        )

        movement_type = "ajuste"
        movement_reason = f"{reason} ({'entrada' if signed_qty > 0 else 'saida'})"

        c.execute(
            """
            INSERT INTO stock_movements(
                product_id,
                type,
                qty,
                reason,
                reference,
                user_id,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                data.product_id,
                movement_type,
                qty,
                movement_reason,
                "AJUSTE",
                user["id"],
                now(),
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "stock_adjust",
            "product",
            data.product_id,
            (
                f"produto={product['name']}; "
                f"quantidade={signed_qty:+g}; "
                f"antes={product['stock']}; "
                f"depois={new_stock}; "
                f"motivo={reason}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "stock": new_stock,
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception:
        c.rollback()
        c.close()
        raise


# ============================================================
# DEFINIR / ZERAR ESTOQUE
# ============================================================

@app.post("/api/stock/set")
def set_stock(
    data: StockSetIn,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    validate_non_negative(
        data.stock,
        "Estoque nÃ£o pode ser negativo",
    )

    reason = clean_text(
        data.reason,
        200,
    ) or "Estoque definido manualmente"

    c = conn()

    product = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            data.product_id,
            user["company_id"],
        ),
    ).fetchone()

    if not product:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    new_stock = validate_quantity(
        data.stock,
        product["unit"],
        allow_zero=True,
    )

    old_stock = money(
        product["stock"]
    )

    difference = money(
        new_stock - old_stock
    )

    if abs(difference) < 0.000001:
        c.close()

        return {
            "ok": True,
            "stock": new_stock,
            "message":
                "O estoque jÃ¡ estava nesse valor.",
        }

    try:
        c.execute("BEGIN")

        c.execute(
            """
            UPDATE products
            SET stock=?
            WHERE id=?
              AND company_id=?
            """,
            (
                new_stock,
                data.product_id,
                user["company_id"],
            ),
        )

        movement_type = "ajuste"
        movement_reason = f"{reason} ({'entrada' if difference > 0 else 'saida'})"

        c.execute(
            """
            INSERT INTO stock_movements(
                product_id,
                type,
                qty,
                reason,
                reference,
                user_id,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                data.product_id,
                movement_type,
                abs(difference),
                movement_reason,
                "DEFINICAO-ESTOQUE",
                user["id"],
                now(),
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "stock_set",
            "product",
            data.product_id,
            (
                f"produto={product['name']}; "
                f"antes={old_stock}; "
                f"depois={new_stock}; "
                f"motivo={reason}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "stock": new_stock,
            "difference": difference,
        }

    except Exception:
        c.rollback()
        c.close()
        raise


@app.post("/api/stock/zero/{product_id}")
def zero_stock(
    product_id: int,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    c = conn()

    product = c.execute(
        """
        SELECT *
        FROM products
        WHERE id=?
          AND company_id=?
        """,
        (
            product_id,
            user["company_id"],
        ),
    ).fetchone()

    if not product:
        c.close()

        raise HTTPException(
            404,
            "Produto nÃ£o encontrado",
        )

    old_stock = money(
        product["stock"]
    )

    if old_stock <= 0:
        c.close()

        return {
            "ok": True,
            "stock": 0,
            "message":
                "O estoque jÃ¡ estÃ¡ zerado.",
        }

    try:
        c.execute("BEGIN")

        c.execute(
            """
            UPDATE products
            SET stock=0
            WHERE id=?
              AND company_id=?
            """,
            (
                product_id,
                user["company_id"],
            ),
        )

        c.execute(
            """
            INSERT INTO stock_movements(
                product_id,
                type,
                qty,
                reason,
                reference,
                user_id,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                product_id,
                "saida",
                old_stock,
                "Estoque zerado manualmente",
                "ZERAR-ESTOQUE",
                user["id"],
                now(),
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "stock_zero",
            "product",
            product_id,
            (
                f"produto={product['name']}; "
                f"estoque_anterior={old_stock}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "stock": 0,
            "removed": old_stock,
        }

    except Exception:
        c.rollback()
        c.close()
        raise


@app.get("/api/stock/movements")
def stock_movements(request: Request):
    user = require_user(request)

    c = conn()

    if user["role"] in MANAGEMENT_ROLES:

        rows = c.execute(
            """
            SELECT
                sm.*,
                p.name product,
                u.name user_name
            FROM stock_movements sm
            JOIN products p
                ON p.id=sm.product_id
            JOIN users u
                ON u.id=sm.user_id
            WHERE sm.company_id=?
            ORDER BY sm.id DESC
            LIMIT 500
            """,
            (user["company_id"],),
        ).fetchall()

    else:

        rows = c.execute(
            """
            SELECT
                sm.*,
                p.name product,
                u.name user_name
            FROM stock_movements sm
            JOIN products p
                ON p.id=sm.product_id
            JOIN users u
                ON u.id=sm.user_id
            WHERE sm.user_id=?
              AND sm.company_id=?
            ORDER BY sm.id DESC
            LIMIT 300
            """,
            (
                user["id"],
                user["company_id"],
            ),
        ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


# ============================================================
# CUSTOMERS / SUPPLIERS
# ============================================================

def party_list(table, company_id):
    allowed = {
        "customers",
        "suppliers",
    }

    if table not in allowed:
        raise ValueError(
            "Tabela inv?lida"
        )

    c = conn()

    rows = c.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE company_id=?
        ORDER BY name
        """,
        (company_id,),
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]

def party_create(
    table,
    data,
    user,
):
    allowed = {
        "customers",
        "suppliers",
    }

    if table not in allowed:
        raise ValueError(
            "Tabela invÃ¡lida"
        )

    name = clean_text(
        data.name,
        150,
    )

    if not name:
        raise HTTPException(
            400,
            "Nome Ã© obrigatÃ³rio",
        )

    c = conn()

    cur = c.execute(
        f"""
        INSERT INTO {table}(
            name,
            cpf_cnpj,
            phone,
            email,
            address,
            notes,
            created_at,
            company_id
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            name,
            clean_text(
                data.cpf_cnpj,
                30,
            ),
            clean_text(
                data.phone,
                30,
            ),
            clean_text(
                data.email,
                150,
            ),
            clean_text(
                data.address,
                250,
            ),
            clean_text(
                data.notes,
                500,
            ),
            now(),
            user["company_id"],
        ),
    )

    entity = table[:-1]

    audit(
        user["id"],
        "create",
        entity,
        cur.lastrowid,
        name,
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "id": cur.lastrowid,
    }


@app.get("/api/customers")
def customers(request: Request):
    user = require_user(request)

    return party_list(
        "customers",
        user["company_id"],
    )


@app.post("/api/customers")
def customer_create(
    data: PartyIn,
    request: Request,
):
    user = require_role(
        request,
        SELLER_ROLES,
    )

    return party_create(
        "customers",
        data,
        user,
    )


@app.get("/api/suppliers")
def suppliers(request: Request):
    user = _store_user(request, MANAGEMENT_ROLES)

    return party_list(
        "suppliers",
        user["company_id"],
    )


@app.post("/api/suppliers")
def supplier_create(
    data: PartyIn,
    request: Request,
):
    user = _store_user(request, MANAGEMENT_ROLES)

    return party_create(
        "suppliers",
        data,
        user,
    )


# ============================================================
# CASH
# ============================================================

def current_cash(
    c,
    user_id=None,
    company_id=None,
):
    sql = """
        SELECT
            cs.*,
            u.name user_name
        FROM cash_sessions cs
        JOIN users u
            ON u.id=cs.user_id
        WHERE cs.status='open'
    """

    params = []

    if company_id is not None:
        sql += """
            AND cs.company_id=?
        """
        params.append(
            company_id
        )

    if user_id is not None:
        sql += """
            AND cs.user_id=?
        """
        params.append(
            user_id
        )

    sql += """
        ORDER BY cs.id DESC
        LIMIT 1
    """

    return c.execute(
        sql,
        params,
    ).fetchone()


def cash_expected(
    c,
    cash_id,
):
    total = c.execute(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN type IN (
                            'venda',
                            'suprimento',
                            'entrada'
                        )
                        THEN amount
                        ELSE -amount
                    END
                ),
                0
            ) total
        FROM cash_movements
        WHERE cash_session_id=?
        AND (
            payment_method IS NULL
            OR payment_method='DINHEIRO'
        )
        """,
        (cash_id,),
    ).fetchone()["total"]

    opening_row = c.execute(
        """
        SELECT
            opening_amount
        FROM cash_sessions
        WHERE id=?
        """,
        (cash_id,),
    ).fetchone()

    if not opening_row:
        return 0

    opening = opening_row[
        "opening_amount"
    ]

    return money(
        opening + total
    )


@app.get("/api/cash/current")
def cash_current(
    request: Request,
):
    user = require_user(request)

    c = conn()

    row = current_cash(
        c,
        user["id"],
        user["company_id"],
    )

    if not row:
        c.close()
        return None

    expected = cash_expected(
        c,
        row["id"],
    )

    result = dict(row)

    result[
        "expected_amount"
    ] = expected

    c.close()

    return result


@app.post("/api/cash/open")
def cash_open(
    data: CashOpenIn,
    request: Request,
):
    user = require_role(
        request,
        SELLER_ROLES,
    )

    validate_non_negative(
        data.opening_amount,
        "Fundo inicial nÃ£o pode ser negativo",
    )

    c = conn()

    if current_cash(
        c,
        user["id"],
        user["company_id"],
    ):
        c.close()

        raise HTTPException(
            400,
            "VocÃª jÃ¡ possui um caixa aberto",
        )

    cur = c.execute(
        """
        INSERT INTO cash_sessions(
            user_id,
            opened_at,
            opening_amount,
            status,
            company_id
        )
        VALUES(?,?,?,?,?)
        """,
        (
            user["id"],
            now(),
            money(data.opening_amount),
            "open",
            user["company_id"],
        ),
    )

    audit(
        user["id"],
        "open_cash",
        "cash_session",
        cur.lastrowid,
        str(
            money(
                data.opening_amount
            )
        ),
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "id": cur.lastrowid,
        "opening_amount":
            money(data.opening_amount),
    }


@app.post("/api/cash/close")
def cash_close(
    data: CashCloseIn,
    request: Request,
):
    user = require_role(
        request,
        SELLER_ROLES,
    )

    validate_non_negative(
        data.closing_amount,
        "Valor contado nÃ£o pode ser negativo",
    )

    c = conn()

    row = current_cash(
        c,
        user["id"],
        user["company_id"],
    )

    if not row:
        c.close()

        raise HTTPException(
            400,
            "Nenhum caixa aberto",
        )

    expected = cash_expected(
        c,
        row["id"],
    )

    counted = money(
        data.closing_amount
    )

    difference = money(
        counted - expected
    )

    c.execute(
        """
        UPDATE cash_sessions
        SET
            closed_at=?,
            closing_amount=?,
            status='closed'
        WHERE id=?
        AND company_id=?
        """,
        (
            now(),
            counted,
            row["id"],
            user["company_id"],
        ),
    )

    audit(
        user["id"],
        "close_cash",
        "cash_session",
        row["id"],
        (
            f"esperado={expected}; "
            f"contado={counted}; "
            f"diferenca={difference}"
        ),
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "expected_amount": expected,
        "closing_amount": counted,
        "difference": difference,
    }


@app.post("/api/cash/move")
def cash_move(
    data: CashMoveIn,
    request: Request,
):
    user = require_role(
        request,
        SELLER_ROLES,
    )

    move_type = clean_text(
        data.type,
        30,
    )

    if not move_type or move_type.lower() not in CASH_TYPES:
        raise HTTPException(
            400,
            "Tipo de movimentaÃ§Ã£o invÃ¡lido",
        )

    move_type = move_type.lower()

    validate_positive(
        data.amount,
        "O valor deve ser maior que zero",
    )

    c = conn()

    row = current_cash(
        c,
        user["id"],
        user["company_id"],
    )

    if not row:
        c.close()

        raise HTTPException(
            400,
            "Abra o caixa primeiro",
        )

    amount = money(
        data.amount
    )

    if move_type in {
        "sangria",
        "saida",
    }:

        expected = cash_expected(
            c,
            row["id"],
        )

        if amount > expected:
            c.close()

            raise HTTPException(
                400,
                "Saldo de caixa insuficiente",
            )

    c.execute(
        """
        INSERT INTO cash_movements(
            cash_session_id,
            user_id,
            type,
            amount,
            payment_method,
            description,
            created_at
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            row["id"],
            user["id"],
            move_type,
            amount,
            "DINHEIRO",
            clean_text(
                data.description,
                250,
            ),
            now(),
        ),
    )

    audit(
        user["id"],
        "cash_move",
        "cash_session",
        row["id"],
        f"{move_type} {amount}",
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True
    }


@app.get("/api/cash/movements")
def cash_moves(
    request: Request,
):
    user = require_user(request)

    c = conn()

    if user["role"] in MANAGEMENT_ROLES:

        rows = c.execute(
            """
            SELECT
                cm.*,
                u.name user_name
            FROM cash_movements cm
            JOIN users u
                ON u.id=cm.user_id
            JOIN cash_sessions cs
                ON cs.id=cm.cash_session_id
            WHERE cs.company_id=?
            ORDER BY cm.id DESC
            LIMIT 500
            """,
            (user["company_id"],),
        ).fetchall()

    else:

        rows = c.execute(
            """
            SELECT
                cm.*,
                u.name user_name
            FROM cash_movements cm
            JOIN users u
                ON u.id=cm.user_id
            JOIN cash_sessions cs
                ON cs.id=cm.cash_session_id
            WHERE cm.user_id=?
            AND cs.company_id=?
            ORDER BY cm.id DESC
            LIMIT 300
            """,
            (
                user["id"],
                user["company_id"],
            ),
        ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


# ============================================================
# SALES
# ============================================================

@app.post("/api/sales")
def create_sale(
    data: SaleIn,
    request: Request,
):
    user = _store_user(request, SELLER_ROLES)

    if not data.items:
        raise HTTPException(
            400,
            "Venda sem itens",
        )

    payment = clean_text(
        data.payment_method,
        30,
    )

    if not payment or payment.upper() not in PAYMENTS:
        raise HTTPException(
            400,
            "Forma de pagamento invÃ¡lida",
        )

    payment = payment.upper()

    validate_non_negative(
        data.discount,
        "Desconto nÃ£o pode ser negativo",
    )

    c = conn()

    cash = current_cash(
        c,
        user["id"],
        user["company_id"],
    )

    if not cash:
        c.close()

        raise HTTPException(
            400,
            "Abra o caixa antes de vender",
        )

    try:
        c.execute("BEGIN")

        subtotal = 0
        prepared = []

        for item in data.items:

            if item.product_id <= 0:
                raise HTTPException(
                    400,
                    "Produto invÃ¡lido",
                )

            product = c.execute(
                """
                SELECT *
                FROM products
                WHERE id=?
                AND company_id=?
                AND active=1
                """,
                (
                    item.product_id,
                    user["company_id"],
                ),
            ).fetchone()

            if not product:
                raise HTTPException(
                    400,
                    (
                        f"Produto {item.product_id} "
                        "nÃ£o encontrado ou inativo"
                    ),
                )

            qty = validate_quantity(
                item.qty,
                product["unit"],
            )

            if product["stock"] < qty:
                raise HTTPException(
                    400,
                    (
                        "Estoque insuficiente: "
                        f"{product['name']} "
                        f"(disponÃ­vel: {product['stock']})"
                    ),
                )

            if item.unit_price is None:
                price = money(
                    product["price"]
                )
            else:
                if not is_finite(
                    item.unit_price
                ):
                    raise HTTPException(
                        400,
                        "PreÃ§o invÃ¡lido",
                    )

                price = money(
                    item.unit_price
                )

                if price < 0:
                    raise HTTPException(
                        400,
                        "PreÃ§o invÃ¡lido",
                    )

                if (
                    abs(
                        price
                        - money(
                            product["price"]
                        )
                    ) > 0.001
                    and user["role"]
                    not in MANAGEMENT_ROLES
                ):
                    raise HTTPException(
                        403,
                        (
                            "Somente gerente ou "
                            "administrador pode "
                            "alterar o preÃ§o da venda"
                        ),
                    )

            line_total = money(
                qty * price
            )

            subtotal += line_total

            prepared.append(
                (
                    product,
                    qty,
                    price,
                    line_total,
                )
            )

        # Protecao multiempresa:
        # o cliente informado na venda precisa pertencer
        # a mesma empresa do usuario autenticado.
        if data.customer_id is not None:
            customer = c.execute(
                """
                SELECT id
                FROM customers
                WHERE id=?
                AND company_id=?
                """,
                (
                    data.customer_id,
                    user["company_id"],
                ),
            ).fetchone()

            if not customer:
                raise HTTPException(
                    400,
                    "Cliente invalido para esta empresa",
                )

        subtotal = money(
            subtotal
        )

        discount = money(
            data.discount
        )

        if discount > 0 and user["role"] not in MANAGEMENT_ROLES:
            raise HTTPException(
                403,
                "Somente gerente ou administrador pode aplicar descontos",
            )

        if discount > subtotal:
            raise HTTPException(
                400,
                "Desconto nÃ£o pode ser maior que o subtotal",
            )

        total = money(
            subtotal - discount
        )

        cur = c.execute(
            """
            INSERT INTO sales(
                customer_id,
                user_id,
                cash_session_id,
                subtotal,
                discount,
                total,
                payment_method,
                status,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.customer_id,
                user["id"],
                cash["id"],
                subtotal,
                discount,
                total,
                payment,
                "completed",
                now(),
                user["company_id"],
            ),
        )

        sale_id = cur.lastrowid

        for (
            product,
            qty,
            price,
            line_total,
        ) in prepared:

            c.execute(
                """
                INSERT INTO sale_items(
                    sale_id,
                    product_id,
                    qty,
                    unit_price,
                    total,
                    cost
                )
                VALUES(?,?,?,?,?,?)
                """,
                (
                    sale_id,
                    product["id"],
                    qty,
                    price,
                    line_total,
                    money(product["cost"]),
                ),
            )

            updated = c.execute(
                """
                UPDATE products
                SET stock=stock-?
                WHERE id=?
                AND company_id=?
                AND stock>=?
                """,
                (
                    qty,
                    product["id"],
                    user["company_id"],
                    qty,
                ),
            ).rowcount

            if updated != 1:
                raise HTTPException(
                    400,
                    (
                        "Estoque insuficiente: "
                        f"{product['name']}"
                    ),
                )

            c.execute(
                """
                INSERT INTO stock_movements(
                    product_id,
                    type,
                    qty,
                    reason,
                    reference,
                    user_id,
                    created_at,
                    company_id
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    product["id"],
                    "venda",
                    qty,
                    "Venda",
                    f"VENDA-{sale_id}",
                    user["id"],
                    now(),
                    user["company_id"],
                ),
            )

        if payment in PHYSICAL_PAYMENT:

            c.execute(
                """
                INSERT INTO cash_movements(
                    cash_session_id,
                    user_id,
                    type,
                    amount,
                    payment_method,
                    description,
                    reference,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    cash["id"],
                    user["id"],
                    "venda",
                    total,
                    payment,
                    (
                        f"Venda #{sale_id} "
                        f"- {payment}"
                    ),
                    f"VENDA-{sale_id}",
                    now(),
                ),
            )

        audit(
            user["id"],
            "create",
            "sale",
            sale_id,
            (
                f"total={total};"
                f"payment={payment}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "sale_id": sale_id,
            "total": total,
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception as e:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            (
                "Erro ao registrar venda: "
                f"{str(e)}"
            ),
        )


@app.get("/api/sales")
def list_sales(
    request: Request,
):
    user = require_user(request)

    c = conn()

    if user["role"] in MANAGEMENT_ROLES:

        rows = c.execute(
            """
            SELECT
                s.*,
                COALESCE(
                    cu.name,
                    'Consumidor'
                ) customer,
                u.name user_name
            FROM sales s
            LEFT JOIN customers cu
                ON cu.id=s.customer_id
            JOIN users u
                ON u.id=s.user_id
            WHERE s.company_id=?
            ORDER BY s.id DESC
            LIMIT 500
            """,
            (user["company_id"],),
        ).fetchall()

    else:

        rows = c.execute(
            """
            SELECT
                s.*,
                COALESCE(
                    cu.name,
                    'Consumidor'
                ) customer,
                u.name user_name
            FROM sales s
            LEFT JOIN customers cu
                ON cu.id=s.customer_id
            JOIN users u
                ON u.id=s.user_id
            WHERE s.user_id=?
            AND s.company_id=?
            ORDER BY s.id DESC
            LIMIT 300
            """,
            (
                user["id"],
                user["company_id"],
            ),
        ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


@app.get("/api/sales/{sid}")
def sale_detail(
    sid: int,
    request: Request,
):
    user = require_user(request)

    c = conn()

    sale = c.execute(
        """
        SELECT
            s.*,
            COALESCE(
                cu.name,
                'Consumidor'
            ) customer,
            u.name user_name
        FROM sales s
        LEFT JOIN customers cu
            ON cu.id=s.customer_id
        JOIN users u
            ON u.id=s.user_id
        WHERE s.id=?
        AND s.company_id=?
        """,
        (
            sid,
            user["company_id"],
        ),
    ).fetchone()

    if not sale:
        c.close()

        raise HTTPException(
            404,
            "Venda nÃ£o encontrada",
        )

    if (
        user["role"]
        not in MANAGEMENT_ROLES
        and sale["user_id"]
        != user["id"]
    ):
        c.close()

        raise HTTPException(
            403,
            "VocÃª nÃ£o pode acessar esta venda",
        )

    items = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                si.*,
                p.name product,
                p.sku,
                p.barcode
            FROM sale_items si
            JOIN products p
                ON p.id=si.product_id
                AND p.company_id=?
            WHERE si.sale_id=?
            """,
            (
                user["company_id"],
                sid,
            ),
        ).fetchall()
    ]

    c.close()

    return {
        "sale": dict(sale),
        "items": items,
    }


# ============================================================
# CANCELAMENTO / ESTORNO
# ============================================================

@app.post("/api/sales/{sid}/cancel")
def cancel_sale(
    sid: int,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    c = conn()

    sale = c.execute(
        """
        SELECT *
        FROM sales
        WHERE id=?
        AND company_id=?
        """,
        (
            sid,
            user["company_id"],
        ),
    ).fetchone()

    if not sale:
        c.close()

        raise HTTPException(
            404,
            "Venda nÃ£o encontrada",
        )

    if sale["status"] != "completed":
        c.close()

        raise HTTPException(
            400,
            "Esta venda jÃ¡ foi cancelada",
        )

    try:
        c.execute("BEGIN")

        # Se foi venda em dinheiro,
        # o caixa original precisa estar aberto.
        if (
            sale["payment_method"]
            in PHYSICAL_PAYMENT
        ):
            cash_id = sale[
                "cash_session_id"
            ]

            if not cash_id:
                raise HTTPException(
                    400,
                    "A venda em dinheiro nÃ£o possui caixa vinculado",
                )

            cash_session = c.execute(
                """
                SELECT *
                FROM cash_sessions
                WHERE id=?
                AND company_id=?
                """,
                (
                    cash_id,
                    user["company_id"],
                ),
            ).fetchone()

            if not cash_session:
                raise HTTPException(
                    400,
                    "O caixa da venda nÃ£o foi encontrado",
                )

            if cash_session["status"] != "open":
                raise HTTPException(
                    400,
                    (
                        "Esta venda em dinheiro "
                        "pertence a um caixa jÃ¡ fechado. "
                        "NÃ£o Ã© possÃ­vel alterar um caixa encerrado."
                    ),
                )

        items = c.execute(
            """
            SELECT *
            FROM sale_items
            WHERE sale_id=?
            """,
            (sid,),
        ).fetchall()

        for item in items:

            c.execute(
                """
                UPDATE products
                SET stock=stock+?
                WHERE id=?
                AND company_id=?
                """,
                (
                    item["qty"],
                    item["product_id"],
                    user["company_id"],
                ),
            )

            c.execute(
                """
                INSERT INTO stock_movements(
                    product_id,
                    type,
                    qty,
                    reason,
                    reference,
                    user_id,
                    created_at,
                    company_id
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    item["product_id"],
                    "entrada",
                    item["qty"],
                    "Estorno de venda",
                    f"ESTORNO-{sid}",
                    user["id"],
                    now(),
                    user["company_id"],
                ),
            )

        if (
            sale["payment_method"]
            in PHYSICAL_PAYMENT
        ):

            c.execute(
                """
                INSERT INTO cash_movements(
                    cash_session_id,
                    user_id,
                    type,
                    amount,
                    payment_method,
                    description,
                    reference,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    sale["cash_session_id"],
                    user["id"],
                    "saida",
                    money(
                        sale["total"]
                    ),
                    "DINHEIRO",
                    (
                        f"Estorno da venda "
                        f"#{sid}"
                    ),
                    f"ESTORNO-VENDA-{sid}",
                    now(),
                ),
            )

        c.execute(
            """
            UPDATE sales
            SET status='cancelled'
            WHERE id=?
            AND company_id=?
            """,
            (
                sid,
                user["company_id"],
            ),
        )

        audit(
            user["id"],
            "cancel",
            "sale",
            sid,
            (
                f"total={sale['total']};"
                f"payment={sale['payment_method']}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "sale_id": sid,
            "message":
                "Venda cancelada e estoque restaurado.",
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception as e:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            (
                "Erro ao cancelar venda: "
                f"{str(e)}"
            ),
        )


# ============================================================
# PURCHASES
# ============================================================

@app.post("/api/purchases")
def create_purchase(
    data: PurchaseIn,
    request: Request,
):
    user = _store_user(request, MANAGEMENT_ROLES)

    payment = clean_text(
        data.payment_method,
        30,
    )

    if not payment or payment.upper() not in PAYMENTS:
        raise HTTPException(
            400,
            "Forma de pagamento invÃ¡lida",
        )

    payment = payment.upper()

    if not data.items:
        raise HTTPException(
            400,
            "Compra sem itens",
        )

    c = conn()

    try:
        c.execute("BEGIN")

        total = 0
        prepared = []

        for item in data.items:

            if item.product_id <= 0:
                raise HTTPException(
                    400,
                    "Produto invÃ¡lido",
                )

            product = c.execute(
                """
                SELECT *
                FROM products
                WHERE id=?
                AND company_id=?
                """,
                (
                    item.product_id,
                    user["company_id"],
                ),
            ).fetchone()

            if not product:
                raise HTTPException(
                    400,
                    "Produto invÃ¡lido",
                )

            qty = validate_quantity(
                item.qty,
                product["unit"],
            )

            if not is_finite(
                item.unit_cost
            ):
                raise HTTPException(
                    400,
                    "Custo invÃ¡lido",
                )

            if item.unit_cost < 0:
                raise HTTPException(
                    400,
                    "Custo invÃ¡lido",
                )

            unit_cost = money(
                item.unit_cost
            )

            line_total = money(
                qty * unit_cost
            )

            total += line_total

            prepared.append(
                (
                    product,
                    qty,
                    unit_cost,
                    line_total,
                )
            )

        total = money(
            total
        )

        # Protecao multiempresa:
        # o fornecedor informado na compra precisa pertencer
        # a mesma empresa do usuario autenticado.
        if data.supplier_id is not None:
            supplier = c.execute(
                """
                SELECT id
                FROM suppliers
                WHERE id=?
                AND company_id=?
                """,
                (
                    data.supplier_id,
                    user["company_id"],
                ),
            ).fetchone()

            if not supplier:
                raise HTTPException(
                    400,
                    "Fornecedor invalido para esta empresa",
                )

        cur = c.execute(
            """
            INSERT INTO purchases(
                supplier_id,
                user_id,
                total,
                invoice_ref,
                payment_method,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                data.supplier_id,
                user["id"],
                total,
                clean_text(
                    data.invoice_ref,
                    100,
                ),
                payment,
                now(),
                user["company_id"],
            ),
        )

        purchase_id = cur.lastrowid

        for (
            product,
            qty,
            unit_cost,
            line_total,
        ) in prepared:

            c.execute(
                """
                INSERT INTO purchase_items(
                    purchase_id,
                    product_id,
                    qty,
                    unit_cost,
                    total
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    purchase_id,
                    product["id"],
                    qty,
                    unit_cost,
                    line_total,
                ),
            )

            c.execute(
                """
                UPDATE products
                SET
                    stock=stock+?,
                    cost=?
                WHERE id=?
                AND company_id=?
                """,
                (
                    qty,
                    unit_cost,
                    product["id"],
                    user["company_id"],
                ),
            )

            c.execute(
                """
                INSERT INTO stock_movements(
                    product_id,
                    type,
                    qty,
                    reason,
                    reference,
                    user_id,
                    created_at,
                    company_id
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    product["id"],
                    "entrada",
                    qty,
                    "Compra",
                    f"COMPRA-{purchase_id}",
                    user["id"],
                    now(),
                    user["company_id"],
                ),
            )

        if payment in PHYSICAL_PAYMENT:

            cash = current_cash(
                c,
                user["id"],
                user["company_id"],
            )

            if not cash:
                raise HTTPException(
                    400,
                    (
                        "Para pagar uma compra "
                        "em dinheiro, abra o caixa primeiro"
                    ),
                )

            expected = cash_expected(
                c,
                cash["id"],
            )

            if total > expected:
                raise HTTPException(
                    400,
                    (
                        "Saldo de caixa "
                        "insuficiente para esta compra"
                    ),
                )

            c.execute(
                """
                INSERT INTO cash_movements(
                    cash_session_id,
                    user_id,
                    type,
                    amount,
                    payment_method,
                    description,
                    reference,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    cash["id"],
                    user["id"],
                    "saida",
                    total,
                    payment,
                    f"Compra #{purchase_id}",
                    f"COMPRA-{purchase_id}",
                    now(),
                ),
            )

        audit(
            user["id"],
            "create",
            "purchase",
            purchase_id,
            (
                f"total={total};"
                f"payment={payment}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "purchase_id": purchase_id,
            "total": total,
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception as e:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            (
                "Erro ao registrar compra: "
                f"{str(e)}"
            ),
        )


@app.get("/api/purchases")
def list_purchases(
    request: Request,
):
    user = _store_user(request, MANAGEMENT_ROLES)

    c = conn()

    rows = c.execute(
        """
        SELECT
            p.*,
            COALESCE(
                s.name,
                'Sem fornecedor'
            ) supplier,
            u.name user_name
        FROM purchases p
        LEFT JOIN suppliers s
            ON s.id=p.supplier_id
        JOIN users u
            ON u.id=p.user_id
        WHERE p.company_id=?
        ORDER BY p.id DESC
        LIMIT 300
        """,
        (
            user["company_id"],
        ),
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


# ============================================================
# EXPENSES
# ============================================================

@app.post("/api/expenses")
def expense_create(
    data: ExpenseIn,
    request: Request,
):
    user = _store_user(request, MANAGEMENT_ROLES)

    payment = (
        clean_text(
            data.payment_method,
            30,
        )
        or "PIX"
    ).upper()

    description = clean_text(
        data.description,
        250,
    )

    if not description:
        raise HTTPException(
            400,
            "DescriÃ§Ã£o da despesa Ã© obrigatÃ³ria",
        )

    if payment not in PAYMENTS:
        raise HTTPException(
            400,
            "Forma de pagamento invÃ¡lida",
        )

    validate_positive(
        data.amount,
        "Valor da despesa deve ser maior que zero",
    )

    c = conn()

    try:
        c.execute("BEGIN")

        amount = money(
            data.amount
        )

        cur = c.execute(
            """
            INSERT INTO expenses(
                description,
                category,
                amount,
                payment_method,
                user_id,
                company_id,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                description,
                clean_text(
                    data.category,
                    100,
                ),
                amount,
                payment,
                user["id"],
                user["company_id"],
                now(),
            ),
        )

        expense_id = cur.lastrowid

        if payment in PHYSICAL_PAYMENT:

            cash = current_cash(
                c,
                user["id"],
            )

            if not cash:
                raise HTTPException(
                    400,
                    (
                        "Para pagar uma despesa "
                        "em dinheiro, abra o caixa primeiro"
                    ),
                )

            expected = cash_expected(
                c,
                cash["id"],
            )

            if amount > expected:
                raise HTTPException(
                    400,
                    (
                        "Saldo de caixa "
                        "insuficiente para esta despesa"
                    ),
                )

            c.execute(
                """
                INSERT INTO cash_movements(
                    cash_session_id,
                    user_id,
                    type,
                    amount,
                    payment_method,
                    description,
                    reference,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    cash["id"],
                    user["id"],
                    "saida",
                    amount,
                    payment,
                    description,
                    f"DESP-{expense_id}",
                    now(),
                ),
            )

        audit(
            user["id"],
            "create",
            "expense",
            expense_id,
            description,
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "id": expense_id,
        }

    except HTTPException:
        c.rollback()
        c.close()
        raise

    except Exception as e:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            (
                "Erro ao registrar despesa: "
                f"{str(e)}"
            ),
        )


@app.get("/api/expenses")
def expenses(
    request: Request,
):
    user = _store_user(request, MANAGEMENT_ROLES)

    c = conn()

    rows = c.execute(
        """
        SELECT
            e.*,
            u.name user_name
        FROM expenses e
        JOIN users u
            ON u.id=e.user_id
            AND u.company_id=e.company_id
        WHERE e.company_id=?
        ORDER BY e.id DESC
        LIMIT 300
        """,
        (
            user["company_id"],
        ),
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


# ============================================================
# REPORTS
# ============================================================

@app.get("/api/reports/summary")
def report_summary(
    request: Request,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    start = start or datetime.now().strftime(
        "%Y-%m-01"
    )

    end = end or datetime.now().strftime(
        "%Y-%m-%d"
    )

    try:
        datetime.strptime(
            start,
            "%Y-%m-%d",
        )

        datetime.strptime(
            end,
            "%Y-%m-%d",
        )

    except ValueError:
        raise HTTPException(
            400,
            "Período inválido",
        )

    if start > end:
        raise HTTPException(
            400,
            "A data inicial não pode ser maior que a final",
        )

    c = conn()

    company_id = user["company_id"]

    sales = c.execute(
        """
        SELECT
            COUNT(*) count,
            COALESCE(
                SUM(total),
                0
            ) total,
            COALESCE(
                SUM(discount),
                0
            ) discounts
        FROM sales
        WHERE status='completed'
        AND company_id=?
        AND date(created_at)
            BETWEEN ? AND ?
        """,
        (
            company_id,
            start,
            end,
        ),
    ).fetchone()

    exp = 0
    if user.get("segment") != "RESTAURANTE":
        exp = c.execute(
            """
            SELECT COALESCE(SUM(amount),0) total
            FROM expenses
            WHERE company_id=? AND date(created_at) BETWEEN ? AND ?
            """,
            (company_id, start, end),
        ).fetchone()["total"]

    cogs = c.execute(
        """
        SELECT
            COALESCE(
                SUM(
                    si.qty * si.cost
                ),
                0
            ) total
        FROM sale_items si
        JOIN sales s
            ON s.id=si.sale_id
        WHERE s.status='completed'
        AND s.company_id=?
        AND date(s.created_at)
            BETWEEN ? AND ?
        """,
        (
            company_id,
            start,
            end,
        ),
    ).fetchone()["total"]

    methods = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                payment_method,
                COUNT(*) count,
                SUM(total) total
            FROM sales
            WHERE status='completed'
            AND company_id=?
            AND date(created_at)
                BETWEEN ? AND ?
            GROUP BY payment_method
            ORDER BY total DESC
            """,
            (
                company_id,
                start,
                end,
            ),
        ).fetchall()
    ]

    daily = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                date(created_at) day,
                COUNT(*) count,
                SUM(total) total
            FROM sales
            WHERE status='completed'
            AND company_id=?
            AND date(created_at)
                BETWEEN ? AND ?
            GROUP BY date(created_at)
            ORDER BY day
            """,
            (
                company_id,
                start,
                end,
            ),
        ).fetchall()
    ]

    top = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                p.name,
                SUM(si.qty) qty,
                SUM(si.total) revenue
            FROM sale_items si
            JOIN sales s
                ON s.id=si.sale_id
            JOIN products p
                ON p.id=si.product_id
            WHERE s.status='completed'
            AND s.company_id=?
            AND p.company_id=?
            AND date(s.created_at)
                BETWEEN ? AND ?
            GROUP BY p.id
            ORDER BY revenue DESC
            LIMIT 20
            """,
            (
                company_id,
                company_id,
                start,
                end,
            ),
        ).fetchall()
    ]

    c.close()

    revenue = money(
        sales["total"]
    )

    expenses_total = money(
        exp
    )

    estimated_cogs = money(
        cogs
    )

    return {
        "start": start,
        "end": end,
        "sales_count":
            sales["count"],
        "revenue":
            revenue,
        "discounts":
            money(
                sales["discounts"]
            ),
        "expenses":
            expenses_total,
        "estimated_cogs":
            estimated_cogs,
        "estimated_profit":
            money(
                revenue
                - expenses_total
                - estimated_cogs
            ),
        "payment_methods":
            methods,
        "daily":
            daily,
        "top_products":
            top,
    }
@app.get("/api/export/{entity}")
def export_csv(
    entity: str,
    request: Request,
):
    user = require_role(
        request,
        MANAGEMENT_ROLES,
    )

    allowed = {
        "products":
            "SELECT * FROM products WHERE company_id=?",

        "customers":
            "SELECT * FROM customers WHERE company_id=?",

        "suppliers":
            "SELECT * FROM suppliers WHERE company_id=?",

        "sales":
            "SELECT * FROM sales WHERE company_id=?",

        "stock_movements":
            "SELECT * FROM stock_movements WHERE company_id=?",

        "expenses":
            "SELECT * FROM expenses WHERE company_id=?",

        "purchases":
            "SELECT * FROM purchases WHERE company_id=?",
    }

    if entity not in allowed:
        raise HTTPException(
            404,
            "ExportaÃ§Ã£o nÃ£o disponÃ­vel",
        )

    if entity in {"suppliers", "expenses", "purchases"}:
        user = _store_user(request, MANAGEMENT_ROLES)

    c = conn()

    rows = c.execute(
        allowed[entity],
        (user["company_id"],)
    ).fetchall()

    c.close()

    output = io.StringIO()

    if rows:

        writer = csv.DictWriter(
            output,
            fieldnames=rows[0].keys(),
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                dict(row)
            )

    return Response(
        content=(
            "\ufeff"
            + output.getvalue()
        ),
        media_type=(
            "text/csv; charset=utf-8"
        ),
        headers={
            "Content-Disposition":
                (
                    "attachment; "
                    f"filename={entity}.csv"
                )
        },
    )


# ============================================================
# USERS
# ============================================================

@app.get("/api/users")
def users(
    request: Request,
):
    admin = require_role(
        request,
        ADMIN_ROLES,
    )

    c = conn()

    rows = c.execute(
        """
        SELECT
            id,
            name,
            username,
            role,
            active,
            created_at,
            company_id
        FROM users
        WHERE company_id=?
        ORDER BY name
        """,
        (admin["company_id"],),
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]


@app.post("/api/users")
def user_create(
    data: UserIn,
    request: Request,
):
    admin = require_role(
        request,
        ADMIN_ROLES,
    )

    name = clean_text(
        data.name,
        150,
    )

    username = normalize_username(
        data.username
    )

    if not name:
        raise HTTPException(
            400,
            "Nome Ã© obrigatÃ³rio",
        )

    if not username:
        raise HTTPException(
            400,
            "UsuÃ¡rio Ã© obrigatÃ³rio",
        )

    role = clean_text(
        data.role,
        30,
    )

    if not role or role.lower() not in VALID_ROLES:
        raise HTTPException(
            400,
            "Perfil invÃ¡lido",
        )

    role = role.lower()

    forbid_master_role_assignment(role)

    if len(data.password) < 6:
        raise HTTPException(
            400,
            "Senha deve ter pelo menos 6 caracteres",
        )

    salt, hp = hash_password(
        data.password
    )

    c = conn()

    try:

        cur = c.execute(
            """
            INSERT INTO users(
                name,
                username,
                password_salt,
                password_hash,
                role,
                active,
                created_at,
                company_id
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                name,
                username,
                salt,
                hp,
                role,
                1 if data.active else 0,
                now(),
                admin["company_id"],
            ),
        )

        audit(
            admin["id"],
            "create",
            "user",
            cur.lastrowid,
            username,
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "id": cur.lastrowid,
        }

    except sqlite3.IntegrityError:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            "Nome de usuÃ¡rio jÃ¡ existe",
        )


@app.put("/api/users/{user_id}")
def user_update(
    user_id: int,
    data: UserUpdateIn,
    request: Request,
):
    admin = require_role(
        request,
        ADMIN_ROLES,
    )

    role = clean_text(
        data.role,
        30,
    )

    if not role or role.lower() not in VALID_ROLES:
        raise HTTPException(
            400,
            "Perfil invÃ¡lido",
        )

    role = role.lower()

    forbid_master_role_assignment(role)

    name = clean_text(
        data.name,
        150,
    )

    username = normalize_username(
        data.username
    )

    if not name or not username:
        raise HTTPException(
            400,
            "Nome e usuÃ¡rio sÃ£o obrigatÃ³rios",
        )

    c = conn()

    target = c.execute(
        """
        SELECT *
        FROM users
        WHERE id=?
        AND company_id=?
        """,
        (
            user_id,
            admin["company_id"],
        ),
    ).fetchone()

    if not target:
        c.close()

        raise HTTPException(
            404,
            "UsuÃ¡rio nÃ£o encontrado",
        )

    # Usuários MASTER não podem ser alterados por admins de empresa.
    if str(target["role"]).lower() == "master":
        c.close()

        raise HTTPException(
            403,
            "Apenas o MASTER pode alterar um usuário master",
        )

    # Impede desativar o prÃ³prio usuÃ¡rio.
    if (
        user_id == admin["id"]
        and not data.active
    ):
        c.close()

        raise HTTPException(
            400,
            "VocÃª nÃ£o pode desativar seu prÃ³prio usuÃ¡rio",
        )

    # Impede remover o prÃ³prio admin.
    if (
        user_id == admin["id"]
        and role != "admin"
    ):
        c.close()

        raise HTTPException(
            400,
            "VocÃª nÃ£o pode remover seu prÃ³prio perfil de administrador",
        )

    # Protege o Ãºltimo administrador ativo.
    if target["role"] == "admin":

        admin_count = c.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role='admin'
            AND active=1
            AND company_id=?
            """
        ,
            (admin["company_id"],),
        ).fetchone()[0]

        removing_admin = (
            role != "admin"
            or not data.active
        )

        if (
            removing_admin
            and admin_count <= 1
        ):
            c.close()

            raise HTTPException(
                400,
                (
                    "NÃ£o Ã© possÃ­vel remover "
                    "ou desativar o Ãºltimo administrador"
                ),
            )

    try:

        if data.password:

            if len(data.password) < 6:
                c.close()

                raise HTTPException(
                    400,
                    "Senha deve ter pelo menos 6 caracteres",
                )

            salt, hp = hash_password(
                data.password
            )

            c.execute(
                """
                UPDATE users
                SET
                    name=?,
                    username=?,
                    role=?,
                    active=?,
                    password_salt=?,
                    password_hash=?
                WHERE id=?
                AND company_id=?
                """,
                (
                    name,
                    username,
                    role,
                    1 if data.active else 0,
                    salt,
                    hp,
                    user_id,
                    admin["company_id"],
                ),
            )

            # MudanÃ§a de senha encerra sessÃµes antigas.
            c.execute(
                """
                DELETE FROM sessions
                WHERE user_id=?
                """,
                (user_id,),
            )

        else:

            c.execute(
                """
                UPDATE users
                SET
                    name=?,
                    username=?,
                    role=?,
                    active=?
                WHERE id=?
                AND company_id=?
                """,
                (
                    name,
                    username,
                    role,
                    1 if data.active else 0,
                    user_id,
                    admin["company_id"],
                ),
            )

        audit(
            admin["id"],
            "update",
            "user",
            user_id,
            (
                f"username={username};"
                f"role={role};"
                f"active={data.active}"
            ),
            c,
        )

        c.commit()
        c.close()

        return {
            "ok": True
        }

    except sqlite3.IntegrityError:
        c.rollback()
        c.close()

        raise HTTPException(
            400,
            "Nome de usuÃ¡rio jÃ¡ existe",
        )


# ============================================================
# SETTINGS
# ============================================================

@app.get("/api/settings")
def settings(
    request: Request,
):
    user = require_user(request)

    c = conn()

    rows = c.execute(
        """
        SELECT
            key,
            value
        FROM settings
        WHERE company_id=?
        """,
        (
            user["company_id"],
        ),
    ).fetchall()

    c.close()

    return {
        r["key"]: r["value"]
        for r in rows
    }
@app.put("/api/settings")
def settings_update(
    data: SettingsIn,
    request: Request,
):
    admin = require_role(
        request,
        ADMIN_ROLES,
    )

    store_name = clean_text(
        data.store_name,
        150,
    )

    if not store_name:
        raise HTTPException(
            400,
            "Nome da loja é obrigatório",
        )

    c = conn()

    values = data.model_dump()

    values["store_name"] = store_name

    company_id = admin["company_id"]

    for key, value in values.items():

        clean_value = clean_text(
            value,
            500,
        ) or ""

        existing = c.execute(
            """
            SELECT id
            FROM settings
            WHERE company_id=?
            AND key=?
            """,
            (
                company_id,
                key,
            ),
        ).fetchone()

        if existing:
            c.execute(
                """
                UPDATE settings
                SET value=?
                WHERE id=?
                AND company_id=?
                """,
                (
                    clean_value,
                    existing["id"],
                    company_id,
                ),
            )

        else:
            c.execute(
                """
                INSERT INTO settings(
                    key,
                    value,
                    company_id
                )
                VALUES(?,?,?)
                """,
                (
                    key,
                    clean_value,
                    company_id,
                ),
            )

    audit(
        admin["id"],
        "update",
        "settings",
        None,
        store_name,
        c,
    )

    c.commit()
    c.close()

    return {
        "ok": True
    }
@app.get("/api/audit")
def audit_list(
    request: Request,
):
    user = require_role(
        request,
        ADMIN_ROLES,
    )

    c = conn()

    rows = c.execute(
        """
        SELECT
            a.*,
            COALESCE(
                u.name,
                'Sistema'
            ) user_name
        FROM audit_log a
        LEFT JOIN users u
            ON u.id=a.user_id
            AND u.company_id=?
        WHERE a.company_id=?
        ORDER BY a.id DESC
        LIMIT 500
        """,
        (
            user["company_id"],
            user["company_id"],
        ),
    ).fetchall()

    c.close()

    return [
        dict(r)
        for r in rows
    ]
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "time": now(),
    }


# ============================================================
# ROTAS GLOBAIS DO MASTER
# ============================================================

# ============================================================
# MASTER CONTROL PRO
# ============================================================

from master_control import register_master
from master_enterprise import register_master_enterprise


register_master(
    app,
    conn=conn,
    get_user=get_user,
    audit=audit,
    hash_password=hash_password,
    now=now,
)

register_master_enterprise(app, conn=conn, get_user=get_user, audit=audit, hash_password=hash_password, now=now)

# === MASTER CONTROL PRO ===

# ============================================================
# MASTER PLUS
# ============================================================

try:
    from master_plus import register_master_plus
    register_master_plus(app)
    print("[MASTER PLUS] camada registrada com sucesso.")
except Exception as _master_plus_error:
    print(
        "[MASTER PLUS] ERRO AO REGISTRAR:",
        repr(_master_plus_error)
    )

# ============================================================
# FIM MASTER PLUS

# ============================================================
# SUPER MASTER OFICIAL
# ============================================================
# O /master continua sendo fornecido pelo master_control.
# master_plus e master_enterprise permanecem como camadas
# legadas durante a migração e não devem criar outro /master.
# ============================================================

SUPER_MASTER_ENABLED = True
SUPER_MASTER_VERSION = "1.0.0"
SUPER_MASTER_CORE = "master_control"
SUPER_MASTER_LEGACY_MODULES = (
    "master_plus",
    "master_enterprise",
)

print(
    f"[SUPER MASTER] oficial ativo | "
    f"core={SUPER_MASTER_CORE} | "
    f"version={SUPER_MASTER_VERSION}"
)

# ============================================================
# FIM SUPER MASTER OFICIAL
# ============================================================

# ============================================================

# ============================================================
# MASTER ULTIMATE
# ============================================================
from master_ultimate import register_master_ultimate

# MASTER ULTIMATE — registro seguro
try:
    register_master_ultimate(app)
except Exception as _master_ultimate_error:
    print(f"[MASTER ULTIMATE] aviso: {_master_ultimate_error}")


# ============================================================
# RESTAURANTE: MESAS, COMANDAS E KDS
# ============================================================

def _restaurant_user(request, roles=None):
    user = require_role(request, roles) if roles else require_user(request)
    if str(user.get("segment") or "LOJA").upper() != "RESTAURANTE":
        raise HTTPException(403, "Este recurso é exclusivo de empresas RESTAURANTE")
    return user, get_user_company_id(user)


def _store_user(request, roles=None):
    user = require_role(request, roles) if roles else require_user(request)
    if str(user.get("segment") or "LOJA").upper() != "LOJA":
        raise HTTPException(403, "Este recurso é exclusivo de empresas LOJA")
    return user


def _company_user(request, roles=None):
    user = require_role(request, roles) if roles else require_user(request)
    return user, get_user_company_id(user)


def _public_client_key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_menu_url(request, token):
    base = os.environ.get("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))
    return base.rstrip("/") + "/menu/" + token


def _open_table_tab(c, company_id, table_id, user_id):
    tab = c.execute("SELECT id,number,status FROM tabs WHERE company_id=? AND table_id=? AND status IN ('open','paying')", (company_id, table_id)).fetchone()
    if tab:
        return tab
    number = c.execute("SELECT COALESCE(MAX(number),0)+1 FROM tabs WHERE company_id=?", (company_id,)).fetchone()[0]
    cur = c.execute("INSERT INTO tabs(company_id,table_id,number,user_id,status,opened_at) VALUES(?,?,?,?,'open',?)", (company_id, table_id, number, user_id, now()))
    return {"id": cur.lastrowid, "number": number, "status": "open"}


@app.get("/menu/{token}", response_class=HTMLResponse)
def public_menu_page(token: str):
    return FileResponse(os.path.join(STATIC_DIR, "menu.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/public/menu/{token}")
def public_menu(token: str):
    c = conn()
    table = c.execute("SELECT t.id,t.company_id,t.name,t.active,co.name company_name FROM restaurant_tables t JOIN companies co ON co.id=t.company_id AND co.active=1 AND co.segment='RESTAURANTE' WHERE t.public_token=?", (token,)).fetchone()
    if not table or not table["active"]:
        c.close()
        raise HTTPException(404, "Cardápio não encontrado")
    settings = c.execute("SELECT value FROM settings WHERE company_id=? AND key='store_name'", (table["company_id"],)).fetchone()
    restaurant_name = (settings["value"] if settings and settings["value"] else table["company_name"])
    rows = c.execute("""SELECT id,name,description,category,price,stock,unit,image_url
        FROM products WHERE company_id=? AND active=1 ORDER BY category COLLATE NOCASE,name COLLATE NOCASE""", (table["company_id"],)).fetchall()
    products = []
    for row in rows:
        product = dict(row)
        pending = c.execute("""SELECT COALESCE(SUM(i.qty),0) FROM tab_items i JOIN tabs tb
            ON tb.id=i.tab_id AND tb.company_id=i.company_id WHERE i.company_id=? AND i.product_id=?
            AND tb.status IN ('open','paying')""", (table["company_id"], row["id"])).fetchone()[0]
        product["available"] = float(product["stock"] or 0) - float(pending or 0) > 0
        product["image_url"] = product["image_url"] if str(product["image_url"] or "").startswith(("https://", "http://", "/static/")) else ""
        products.append(product)
    categories = sorted({(p["category"] or "Outros").strip() or "Outros" for p in products}, key=str.casefold)
    c.close()
    return {"restaurant_name": restaurant_name, "table_name": table["name"], "categories": categories, "products": products}


@app.post("/api/restaurant/tables/{table_id}/qr")
def restaurant_table_qr(table_id: int, data: RestaurantQrIn, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        table = c.execute("SELECT id,name,public_token FROM restaurant_tables WHERE id=? AND company_id=? AND active=1", (table_id, company_id)).fetchone()
        if not table:
            raise HTTPException(404, "Mesa não encontrada")
        token = secrets.token_urlsafe(32) if data.regenerate or not table["public_token"] else table["public_token"]
        if data.regenerate or not table["public_token"]:
            c.execute("UPDATE restaurant_tables SET public_token=? WHERE id=? AND company_id=?", (token, table_id, company_id))
        audit(user["id"], "generate_qr" if not data.regenerate else "regenerate_qr", "restaurant_table", table_id, table["name"], c)
        c.commit()
        return {"ok": True, "table_name": table["name"], "url": _public_menu_url(request, token)}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.get("/api/restaurant/tables/{table_id}/qr.png")
def restaurant_table_qr_png(table_id: int, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    c = conn()
    table = c.execute("SELECT name,public_token FROM restaurant_tables WHERE id=? AND company_id=? AND active=1", (table_id, company_id)).fetchone()
    c.close()
    if not table or not table["public_token"]:
        raise HTTPException(404, "QR Code ainda não gerado para esta mesa")
    image = qrcode.make(_public_menu_url(request, table["public_token"]))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/public/tables/{token}/orders")
def public_table_order(token: str, data: PublicOrderIn, request: Request):
    client_key = _public_client_key(data.client_id)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        table = c.execute("SELECT t.id,t.company_id,t.name FROM restaurant_tables t JOIN companies co ON co.id=t.company_id AND co.active=1 AND co.segment='RESTAURANTE' WHERE t.public_token=? AND t.active=1", (token,)).fetchone()
        if not table:
            raise HTTPException(404, "Mesa não encontrada")
        company_id, table_id = table["company_id"], table["id"]
        prior = c.execute("SELECT s.order_id,o.tab_id FROM restaurant_public_submissions s JOIN restaurant_orders o ON o.id=s.order_id AND o.company_id=s.company_id WHERE s.company_id=? AND s.table_id=? AND s.client_key=? AND s.request_id=?", (company_id, table_id, client_key, data.request_id)).fetchone()
        if prior:
            c.commit()
            return {"ok": True, "duplicate": True, "order_id": prior["order_id"], "tab_id": prior["tab_id"]}
        last = c.execute("SELECT created_at FROM restaurant_public_request_limits WHERE company_id=? AND table_id=? AND client_key=? AND kind='ORDER' ORDER BY created_at DESC LIMIT 1", (company_id, table_id, client_key)).fetchone()
        if last:
            try:
                if (datetime.now() - datetime.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 2:
                    raise HTTPException(429, "Aguarde um instante antes de enviar outro pedido")
            except ValueError:
                pass
        operator = c.execute("SELECT id FROM users WHERE company_id=? AND active=1 AND role IN ('admin','gerente','atendente','caixa') ORDER BY CASE role WHEN 'admin' THEN 1 WHEN 'gerente' THEN 2 ELSE 3 END,id LIMIT 1", (company_id,)).fetchone()
        if not operator:
            raise HTTPException(409, "Restaurante sem usuário operacional ativo")
        tab = c.execute("SELECT id,number,status FROM tabs WHERE company_id=? AND table_id=? AND status IN ('open','paying')", (company_id, table_id)).fetchone()
        if tab and tab["status"] == "paying":
            raise HTTPException(409, "A mesa está aguardando pagamento")
        if not tab:
            tab = _open_table_tab(c, company_id, table_id, operator["id"])
        requested = {}
        resolved = []
        for line in data.items:
            p = c.execute("SELECT id,name,price,stock,unit,active FROM products WHERE id=? AND company_id=?", (line.product_id, company_id)).fetchone()
            if not p or not p["active"]:
                raise HTTPException(400, "Um produto não existe nesta empresa ou está indisponível")
            qty = validate_quantity(line.qty, p["unit"])
            requested[p["id"]] = requested.get(p["id"], 0) + qty
            resolved.append((p, qty, clean_text(line.notes, 500) or ""))
        for product_id, qty in requested.items():
            p = next(x[0] for x in resolved if x[0]["id"] == product_id)
            pending = c.execute("""SELECT COALESCE(SUM(i.qty),0) FROM tab_items i JOIN tabs t
                ON t.id=i.tab_id AND t.company_id=i.company_id WHERE i.company_id=? AND i.product_id=?
                AND t.status IN ('open','paying')""", (company_id, product_id)).fetchone()[0]
            if float(p["stock"] or 0) - float(pending or 0) < qty:
                raise HTTPException(409, f"Estoque indisponível: {p['name']}")
        order = c.execute("INSERT INTO restaurant_orders(company_id,tab_id,status,notes,user_id,source,created_at,updated_at) VALUES(?,?,'NOVO','',?,'QR',?,?)", (company_id, tab["id"], operator["id"], now(), now()))
        order_id = order.lastrowid
        total = 0.0
        for p, qty, notes in resolved:
            item = c.execute("INSERT INTO tab_items(company_id,tab_id,product_id,product_name,qty,unit_price,notes,added_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (company_id, tab["id"], p["id"], p["name"], qty, money(p["price"]), notes, operator["id"], now()))
            c.execute("INSERT INTO restaurant_order_items(order_id,item_id) VALUES(?,?)", (order_id, item.lastrowid))
            total += qty * float(p["price"])
        c.execute("INSERT INTO restaurant_order_history(company_id,order_id,status,user_id,created_at) VALUES(?,?,'NOVO',?,?)", (company_id, order_id, operator["id"], now()))
        c.execute("INSERT INTO restaurant_public_submissions(company_id,table_id,client_key,request_id,order_id,created_at) VALUES(?,?,?,?,?,?)", (company_id, table_id, client_key, data.request_id, order_id, now()))
        c.execute("INSERT INTO restaurant_public_request_limits(company_id,table_id,client_key,kind,created_at) VALUES(?,?,?,'ORDER',?)", (company_id, table_id, client_key, now()))
        audit(operator["id"], "qr_order", "restaurant_order", order_id, f"Mesa {table['name']}; total={money(total)}", c)
        c.commit()
        result = {"ok": True, "duplicate": False, "order_id": order_id, "tab_id": tab["id"], "tab_number": tab["number"], "total": money(total)}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    notify_company_changed(company_id)
    return result


@app.post("/api/public/tables/{token}/requests")
def public_table_request(token: str, data: PublicTableRequestIn, request: Request):
    kind = clean_text(data.kind, 20).upper()
    if kind not in {"WAITER", "BILL"}:
        raise HTTPException(400, "Solicitação inválida")
    client_key = _public_client_key(data.client_id)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        table = c.execute("SELECT t.id,t.company_id,t.name FROM restaurant_tables t JOIN companies co ON co.id=t.company_id AND co.active=1 AND co.segment='RESTAURANTE' WHERE t.public_token=? AND t.active=1", (token,)).fetchone()
        if not table:
            raise HTTPException(404, "Mesa não encontrada")
        company_id, table_id = table["company_id"], table["id"]
        last = c.execute("SELECT created_at FROM restaurant_public_request_limits WHERE company_id=? AND table_id=? AND client_key=? AND kind=? ORDER BY created_at DESC LIMIT 1", (company_id, table_id, client_key, kind)).fetchone()
        if last:
            try:
                if (datetime.now() - datetime.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 30:
                    raise HTTPException(429, "Esta solicitação já foi enviada. Aguarde o atendimento.")
            except ValueError:
                pass
        tab = c.execute("SELECT id FROM tabs WHERE company_id=? AND table_id=? AND status IN ('open','paying')", (company_id, table_id)).fetchone()
        if kind == "BILL" and not tab:
            raise HTTPException(409, "Não há comanda aberta nesta mesa")
        operator = c.execute("SELECT id FROM users WHERE company_id=? AND active=1 AND role IN ('admin','gerente','atendente','caixa') ORDER BY id LIMIT 1", (company_id,)).fetchone()
        if not operator:
            raise HTTPException(409, "Restaurante sem usuário operacional ativo")
        cur = c.execute("INSERT INTO restaurant_table_requests(company_id,table_id,tab_id,kind,client_key,created_at) VALUES(?,?,?,?,?,?)", (company_id, table_id, tab["id"] if tab else None, kind, client_key, now()))
        c.execute("INSERT INTO restaurant_public_request_limits(company_id,table_id,client_key,kind,created_at) VALUES(?,?,?,?,?)", (company_id, table_id, client_key, kind, now()))
        audit(operator["id"], "qr_request", "restaurant_table_request", cur.lastrowid, f"Mesa {table['name']}; {kind}", c)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    label = "chamou o garçom" if kind == "WAITER" else "pediu a conta"
    notify_company_changed(company_id, "restaurant_request", {"table_name": table["name"], "request_kind": kind})
    return {"ok": True, "message": f"Mesa {table['name']} {label}.", "id": cur.lastrowid}


@app.get("/api/restaurant/requests")
def restaurant_table_requests(request: Request, status: str = "OPEN"):
    user, company_id = _restaurant_user(request)
    status = clean_text(status, 20).upper()
    if status not in {"OPEN", "RESOLVED", "ALL"}:
        raise HTTPException(400, "Filtro inválido")
    c = conn()
    where = "r.company_id=?" + ("" if status == "ALL" else " AND r.status=?")
    params = [company_id] if status == "ALL" else [company_id, status]
    rows = c.execute(f"SELECT r.id,r.kind,r.status,r.created_at,r.tab_id,t.name table_name,tb.number tab_number FROM restaurant_table_requests r JOIN restaurant_tables t ON t.id=r.table_id AND t.company_id=r.company_id LEFT JOIN tabs tb ON tb.id=r.tab_id AND tb.company_id=r.company_id WHERE {where} ORDER BY r.id DESC LIMIT 100", params).fetchall()
    c.close()
    return [dict(row) for row in rows]


@app.post("/api/restaurant/requests/{request_id}/resolve")
def restaurant_table_request_resolve(request_id: int, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        changed = c.execute("UPDATE restaurant_table_requests SET status='RESOLVED',resolved_at=?,resolved_by=? WHERE id=? AND company_id=? AND status='OPEN'", (now(), user["id"], request_id, company_id)).rowcount
        if not changed:
            raise HTTPException(404, "Solicitação não encontrada ou já atendida")
        audit(user["id"], "resolve", "restaurant_table_request", request_id, "", c)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    notify_company_changed(company_id)
    return {"ok": True}


@app.get("/api/restaurant/categories")
def restaurant_categories(request: Request):
    user, company_id = _company_user(request)
    c = conn()
    rows = c.execute("SELECT id,name,active FROM restaurant_categories WHERE company_id=? AND active=1 ORDER BY name COLLATE NOCASE", (company_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


@app.post("/api/restaurant/categories")
def restaurant_category_create(data: RestaurantCategoryIn, request: Request):
    user, company_id = _company_user(request, MANAGEMENT_ROLES)
    name = clean_text(data.name, 100)
    if not name:
        raise HTTPException(400, "Nome da categoria é obrigatório")
    c = conn()
    try:
        cur = c.execute("INSERT INTO restaurant_categories(company_id,name,created_at) VALUES(?,?,?)", (company_id,name,now()))
        audit(user["id"],"create","restaurant_category",cur.lastrowid,name,c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok":True,"id":cur.lastrowid}
    except sqlite3.IntegrityError:
        c.rollback()
        raise HTTPException(409,"Esta categoria já existe nesta empresa")
    finally:
        c.close()


@app.get("/api/restaurant/tables")
def restaurant_tables(request: Request):
    user, company_id = _restaurant_user(request)
    c = conn()
    rows = c.execute("""
        SELECT t.id,t.name,t.active,
               tb.id tab_id,tb.number tab_number,tb.status tab_status,
               tb.opened_at,tb.customer_id,
               COALESCE(SUM(i.qty*i.unit_price),0) total,
               COUNT(i.id) item_count
        FROM restaurant_tables t
        LEFT JOIN tabs tb ON tb.table_id=t.id AND tb.company_id=t.company_id
            AND tb.status IN ('open','paying')
        LEFT JOIN tab_items i ON i.tab_id=tb.id AND i.company_id=t.company_id
        WHERE t.company_id=? AND (t.active=1 OR tb.id IS NOT NULL)
        GROUP BY t.id,tb.id ORDER BY t.name COLLATE NOCASE
    """, (company_id,)).fetchall()
    c.close()
    result = []
    for row in rows:
        item = dict(row)
        item["status"] = "LIVRE" if not item["tab_id"] else ("AGUARDANDO PAGAMENTO" if item["tab_status"] == "paying" else "OCUPADA")
        result.append(item)
    return result


@app.post("/api/restaurant/tables")
def restaurant_table_create(data: RestaurantTableIn, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    name = clean_text(data.name, 80)
    if not name:
        raise HTTPException(400, "Nome da mesa é obrigatório")
    c = conn()
    try:
        cur = c.execute("INSERT INTO restaurant_tables(company_id,name,created_at) VALUES(?,?,?)", (company_id, name, now()))
        audit(user["id"], "create", "restaurant_table", cur.lastrowid, name, c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok": True, "id": cur.lastrowid}
    except sqlite3.IntegrityError:
        c.rollback()
        raise HTTPException(409, "Já existe uma mesa com esse nome")
    finally:
        c.close()


@app.post("/api/restaurant/tables/{table_id}/open")
def restaurant_tab_open(table_id: int, data: TabOpenIn, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        table = c.execute("SELECT id FROM restaurant_tables WHERE id=? AND company_id=? AND active=1", (table_id, company_id)).fetchone()
        if not table:
            raise HTTPException(404, "Mesa não encontrada")
        if c.execute("SELECT 1 FROM tabs WHERE table_id=? AND company_id=? AND status IN ('open','paying')", (table_id, company_id)).fetchone():
            raise HTTPException(409, "Mesa já está ocupada")
        if data.customer_id is not None and not c.execute("SELECT id FROM customers WHERE id=? AND company_id=?", (data.customer_id, company_id)).fetchone():
            raise HTTPException(400, "Cliente inválido para esta empresa")
        number = c.execute("SELECT COALESCE(MAX(number),0)+1 FROM tabs WHERE company_id=?", (company_id,)).fetchone()[0]
        cur = c.execute("INSERT INTO tabs(company_id,table_id,number,customer_id,user_id,status,opened_at) VALUES(?,?,?,?,?,'open',?)", (company_id, table_id, number, data.customer_id, user["id"], now()))
        audit(user["id"], "open", "tab", cur.lastrowid, f"Mesa {table_id}", c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok": True, "id": cur.lastrowid, "number": number}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.get("/api/restaurant/tabs/{tab_id}")
def restaurant_tab_detail(tab_id: int, request: Request):
    user, company_id = _restaurant_user(request)
    c = conn()
    tab = c.execute("""SELECT tb.*,t.name table_name,COALESCE(cu.name,'') customer_name
        FROM tabs tb JOIN restaurant_tables t ON t.id=tb.table_id AND t.company_id=tb.company_id
        LEFT JOIN customers cu ON cu.id=tb.customer_id AND cu.company_id=tb.company_id
        WHERE tb.id=? AND tb.company_id=?""", (tab_id, company_id)).fetchone()
    if not tab:
        c.close()
        raise HTTPException(404, "Comanda não encontrada")
    items = c.execute("SELECT * FROM tab_items WHERE tab_id=? AND company_id=? ORDER BY id", (tab_id, company_id)).fetchall()
    orders = c.execute("""SELECT o.id,o.status,o.notes,o.source,o.created_at,o.updated_at,u.name user_name,
        GROUP_CONCAT(oi.item_id) item_ids
        FROM restaurant_orders o JOIN users u ON u.id=o.user_id AND u.company_id=o.company_id
        LEFT JOIN restaurant_order_items oi ON oi.order_id=o.id
        WHERE o.tab_id=? AND o.company_id=? GROUP BY o.id HAVING COUNT(oi.item_id)>0 ORDER BY o.id""", (tab_id, company_id)).fetchall()
    c.close()
    result = dict(tab)
    result["items"] = [dict(x) for x in items]
    result["orders"] = [dict(x) for x in orders]
    result["subtotal"] = money(sum(float(x["qty"])*float(x["unit_price"]) for x in items))
    result["total"] = result["subtotal"]
    return result


@app.post("/api/restaurant/tabs/{tab_id}/items")
def restaurant_tab_add_item(tab_id: int, data: TabItemIn, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        tab = c.execute("SELECT id,status FROM tabs WHERE id=? AND company_id=?", (tab_id, company_id)).fetchone()
        if not tab or tab["status"] != "open":
            raise HTTPException(404 if not tab else 409, "Comanda indisponível para novos itens")
        product = c.execute("SELECT id,name,price,unit FROM products WHERE id=? AND company_id=? AND active=1", (data.product_id, company_id)).fetchone()
        if not product:
            raise HTTPException(404, "Produto não encontrado nesta empresa")
        qty = validate_quantity(data.qty, product["unit"])
        item = c.execute("""INSERT INTO tab_items(company_id,tab_id,product_id,product_name,qty,unit_price,notes,added_by,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (company_id, tab_id, product["id"], product["name"], qty, money(product["price"]), clean_text(data.notes, 500) or "", user["id"], now()))
        order = c.execute("INSERT INTO restaurant_orders(company_id,tab_id,status,notes,user_id,created_at,updated_at) VALUES(?,?,'NOVO',?,?,?,?)", (company_id, tab_id, clean_text(data.notes, 500) or "", user["id"], now(), now()))
        c.execute("INSERT INTO restaurant_order_items(order_id,item_id) VALUES(?,?)", (order.lastrowid, item.lastrowid))
        c.execute("INSERT INTO restaurant_order_history(company_id,order_id,status,user_id,created_at) VALUES(?,?,'NOVO',?,?)", (company_id, order.lastrowid, user["id"], now()))
        audit(user["id"], "add_item", "tab", tab_id, f"{product['name']} x {qty}", c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok": True, "item_id": item.lastrowid, "order_id": order.lastrowid}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.patch("/api/restaurant/items/{item_id}")
def restaurant_tab_update_item(item_id: int, data: TabItemUpdateIn, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    c = conn()
    item = c.execute("SELECT i.*,p.unit FROM tab_items i JOIN products p ON p.id=i.product_id AND p.company_id=i.company_id JOIN tabs t ON t.id=i.tab_id AND t.company_id=i.company_id WHERE i.id=? AND i.company_id=? AND t.status='open'", (item_id, company_id)).fetchone()
    if not item:
        c.close()
        raise HTTPException(404, "Item não encontrado ou comanda encerrada")
    qty = validate_quantity(data.qty, item["unit"])
    c.execute("UPDATE tab_items SET qty=?,notes=? WHERE id=? AND company_id=?", (qty, clean_text(data.notes, 500) or "", item_id, company_id))
    c.commit()
    c.close()
    notify_company_changed(company_id)
    return {"ok": True}


@app.delete("/api/restaurant/items/{item_id}")
def restaurant_tab_remove_item(item_id: int, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    c = conn()
    row = c.execute("SELECT i.tab_id FROM tab_items i JOIN tabs t ON t.id=i.tab_id AND t.company_id=i.company_id WHERE i.id=? AND i.company_id=? AND t.status='open'", (item_id, company_id)).fetchone()
    if not row:
        c.close()
        raise HTTPException(404, "Item não encontrado ou comanda encerrada")
    c.execute("DELETE FROM tab_items WHERE id=? AND company_id=?", (item_id, company_id))
    audit(user["id"],"remove_item","tab",row["tab_id"],f"item={item_id}",c)
    c.commit()
    c.close()
    notify_company_changed(company_id)
    return {"ok": True}


@app.post("/api/restaurant/tabs/{tab_id}/transfer")
def restaurant_tab_transfer(tab_id: int, data: TabTransferIn, request: Request):
    user, company_id = _restaurant_user(request, MANAGEMENT_ROLES)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        tab = c.execute("SELECT * FROM tabs WHERE id=? AND company_id=? AND status='open'", (tab_id, company_id)).fetchone()
        dest = c.execute("SELECT id FROM restaurant_tables WHERE id=? AND company_id=? AND active=1", (data.table_id, company_id)).fetchone()
        if not tab or not dest:
            raise HTTPException(404, "Comanda ou mesa não encontrada nesta empresa")
        if c.execute("SELECT 1 FROM tabs WHERE table_id=? AND company_id=? AND status IN ('open','paying')", (data.table_id, company_id)).fetchone():
            raise HTTPException(409, "Mesa de destino já está ocupada")
        c.execute("UPDATE tabs SET table_id=? WHERE id=? AND company_id=?", (data.table_id, tab_id, company_id))
        audit(user["id"], "transfer", "tab", tab_id, f"Mesa {tab['table_id']} -> {data.table_id}", c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok": True}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.post("/api/restaurant/tabs/{tab_id}/paying")
def restaurant_tab_paying(tab_id: int, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    c = conn()
    if not c.execute("SELECT 1 FROM tab_items WHERE tab_id=? AND company_id=?", (tab_id,company_id)).fetchone():
        c.close()
        raise HTTPException(400,"Comanda sem itens")
    changed = c.execute("UPDATE tabs SET status='paying' WHERE id=? AND company_id=? AND status='open'", (tab_id, company_id)).rowcount
    c.commit()
    c.close()
    if not changed:
        raise HTTPException(404, "Comanda não encontrada ou já encerrada")
    notify_company_changed(company_id)
    return {"ok": True}


@app.patch("/api/restaurant/tabs/{tab_id}")
def restaurant_tab_update(tab_id: int, data: TabNotesIn, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    c=conn()
    changed=c.execute("UPDATE tabs SET notes=? WHERE id=? AND company_id=? AND status='open'", (clean_text(data.notes,1000) or "",tab_id,company_id)).rowcount
    c.commit();c.close()
    if not changed:
        raise HTTPException(404,"Comanda não encontrada ou aguardando pagamento")
    notify_company_changed(company_id)
    return {"ok":True}


@app.post("/api/restaurant/tabs/{tab_id}/close")
def restaurant_tab_close(tab_id: int, data: TabCloseIn, request: Request):
    user, company_id = _restaurant_user(request, SELLER_ROLES)
    payment = clean_text(data.payment_method, 30).upper()
    if payment not in PAYMENTS:
        raise HTTPException(400, "Forma de pagamento inválida")
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        tab = c.execute("SELECT * FROM tabs WHERE id=? AND company_id=? AND status IN ('open','paying')", (tab_id, company_id)).fetchone()
        if not tab:
            raise HTTPException(404, "Comanda não encontrada")
        preparing = c.execute("SELECT COUNT(*) FROM restaurant_orders WHERE tab_id=? AND company_id=? AND status IN ('NOVO','EM PREPARAÇÃO')", (tab_id,company_id)).fetchone()[0]
        if preparing:
            raise HTTPException(409,"Aguarde os pedidos em preparo antes de fechar a comanda")
        cash = current_cash(c, user["id"], company_id)
        if not cash:
            raise HTTPException(400, "Abra o caixa antes de fechar a comanda")
        customer_id = data.customer_id if data.customer_id is not None else tab["customer_id"]
        if customer_id is not None and not c.execute("SELECT id FROM customers WHERE id=? AND company_id=?", (customer_id, company_id)).fetchone():
            raise HTTPException(400, "Cliente inválido para esta empresa")
        items = c.execute("SELECT i.*,p.stock,p.cost,p.active FROM tab_items i JOIN products p ON p.id=i.product_id AND p.company_id=i.company_id WHERE i.tab_id=? AND i.company_id=? ORDER BY i.id", (tab_id, company_id)).fetchall()
        if not items:
            raise HTTPException(400, "Comanda sem itens")
        subtotal = money(sum(float(x["qty"])*float(x["unit_price"]) for x in items))
        for item in items:
            if item["stock"] < item["qty"]:
                raise HTTPException(400, f"Estoque insuficiente: {item['product_name']}")
        sale = c.execute("""INSERT INTO sales(customer_id,user_id,cash_session_id,subtotal,discount,total,payment_method,status,created_at,company_id)
            VALUES(?,?,?,?,0,?,?, 'completed',?,?)""", (customer_id,user["id"],cash["id"],subtotal,subtotal,payment,now(),company_id))
        sale_id = sale.lastrowid
        for item in items:
            c.execute("INSERT INTO sale_items(sale_id,product_id,qty,unit_price,total,cost) VALUES(?,?,?,?,?,?)", (sale_id,item["product_id"],item["qty"],item["unit_price"],money(item["qty"]*item["unit_price"]),money(item["cost"])))
            updated = c.execute("UPDATE products SET stock=stock-? WHERE id=? AND company_id=? AND stock>=?", (item["qty"],item["product_id"],company_id,item["qty"])).rowcount
            if updated != 1:
                raise HTTPException(409, f"Estoque alterado durante o fechamento: {item['product_name']}")
            c.execute("INSERT INTO stock_movements(product_id,type,qty,reason,reference,user_id,created_at,company_id) VALUES(?,'venda',?,'Venda',?,?,?,?)", (item["product_id"],item["qty"],f"VENDA-{sale_id}",user["id"],now(),company_id))
        if payment in PHYSICAL_PAYMENT:
            c.execute("INSERT INTO cash_movements(cash_session_id,user_id,type,amount,payment_method,description,reference,created_at) VALUES(?,?,'venda',?,'DINHEIRO',?,?,?)", (cash["id"],user["id"],subtotal,f"Comanda {tab['number']}",f"VENDA-{sale_id}",now()))
        c.execute("UPDATE tabs SET status='closed',closed_at=?,sale_id=?,customer_id=? WHERE id=? AND company_id=?", (now(),sale_id,customer_id,tab_id,company_id))
        audit(user["id"], "close", "tab", tab_id, f"venda={sale_id}; total={subtotal}", c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok": True, "sale_id": sale_id, "total": subtotal}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.get("/api/restaurant/orders")
def restaurant_orders(request: Request, status: Optional[str] = None):
    user, company_id = _restaurant_user(request)
    c = conn()
    params = [company_id]
    where = "o.company_id=? AND o.status IN ('NOVO','EM PREPARAÇÃO','PRONTO')"
    if status:
        status_value = clean_text(status, 30).upper()
        if status_value not in {"NOVO", "EM PREPARAÇÃO", "PRONTO"}:
            c.close()
            raise HTTPException(400, "Status inválido")
        where += " AND o.status=?"
        params.append(status_value)
    rows = c.execute(f"""SELECT o.id,o.tab_id,o.status,o.notes,o.source,o.user_id,o.created_at,o.updated_at,
        t.number tab_number,rt.name table_name,u.name user_name,
        GROUP_CONCAT(oi.item_id) item_ids
        FROM restaurant_orders o JOIN tabs t ON t.id=o.tab_id AND t.company_id=o.company_id
        JOIN restaurant_tables rt ON rt.id=t.table_id AND rt.company_id=t.company_id
        JOIN users u ON u.id=o.user_id AND u.company_id=o.company_id
        LEFT JOIN restaurant_order_items oi ON oi.order_id=o.id
        WHERE {where} GROUP BY o.id HAVING COUNT(oi.item_id)>0 ORDER BY CASE o.status WHEN 'NOVO' THEN 1 WHEN 'EM PREPARAÇÃO' THEN 2 ELSE 3 END,o.created_at""", params).fetchall()
    result=[]
    for row in rows:
        order=dict(row)
        ids=[int(x) for x in (order.pop("item_ids") or "").split(",") if x]
        order["items"]=[]
        if ids:
            marks=",".join("?" for _ in ids)
            order["items"]=[dict(x) for x in c.execute(f"SELECT id,product_name,qty,notes FROM tab_items WHERE company_id=? AND id IN ({marks}) ORDER BY id", [company_id]+ids).fetchall()]
        result.append(order)
    c.close()
    return result


@app.patch("/api/restaurant/orders/{order_id}/status")
def restaurant_order_status(order_id: int, data: OrderStatusIn, request: Request):
    user, company_id = _restaurant_user(request, {"admin","gerente","atendente","cozinha"})
    status_value = clean_text(data.status, 30).upper()
    transitions={"NOVO":"EM PREPARAÇÃO","EM PREPARAÇÃO":"PRONTO","PRONTO":"ENTREGUE"}
    c=conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        order=c.execute("SELECT status FROM restaurant_orders WHERE id=? AND company_id=?", (order_id,company_id)).fetchone()
        if not order:
            raise HTTPException(404,"Pedido não encontrado")
        if transitions.get(order["status"]) != status_value:
            raise HTTPException(409,"Transição de status inválida")
        c.execute("UPDATE restaurant_orders SET status=?,updated_at=? WHERE id=? AND company_id=?", (status_value,now(),order_id,company_id))
        c.execute("INSERT INTO restaurant_order_history(company_id,order_id,status,user_id,created_at) VALUES(?,?,?,?,?)", (company_id,order_id,status_value,user["id"],now()))
        audit(user["id"],"order_status","restaurant_order",order_id,status_value,c)
        c.commit()
        notify_company_changed(company_id)
        return {"ok":True,"status":status_value}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.get("/api/restaurant/orders/{order_id}/history")
def restaurant_order_history(order_id: int, request: Request):
    user, company_id = _restaurant_user(request)
    c=conn()
    rows=c.execute("SELECT h.status,h.created_at,u.name user_name FROM restaurant_order_history h JOIN users u ON u.id=h.user_id AND u.company_id=h.company_id WHERE h.order_id=? AND h.company_id=? ORDER BY h.id", (order_id,company_id)).fetchall()
    exists=c.execute("SELECT 1 FROM restaurant_orders WHERE id=? AND company_id=?", (order_id,company_id)).fetchone()
    c.close()
    if not exists:
        raise HTTPException(404,"Pedido não encontrado")
    return [dict(x) for x in rows]
