# -*- coding: utf-8 -*-

"""
MASTER CONTROL CENTER
Camada administrativa completa do SaaS.

Escopo:
- Empresas
- Usuarios
- Suporte
- Billing
- Planos
- Limites
- Feature Flags
- Seguranca
- Sessoes
- Auditoria
- Analytics
- Alertas
- Diagnostico
- Backups
- Sistema
- Manutencao
- API / Integracoes
- Data Center
"""

import os
import json
import sqlite3
import shutil
import platform
import secrets
import hashlib
import time
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "loja.db"
BACKUP_DIR = BASE_DIR / "BACKUPS"


# ============================================================
# DATABASE
# ============================================================

def db():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 10000")
    return c


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def table_exists(c, table):
    try:
        return bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone())
    except Exception:
        return False


def columns(c, table):
    if not table_exists(c, table):
        return set()

    try:
        return {
            row["name"]
            for row in c.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
        }
    except Exception:
        return set()


def count_rows(c, table, company_id=None):
    if not table_exists(c, table):
        return 0

    try:
        if company_id is not None and "company_id" in columns(c, table):
            return safe_int(
                c.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE company_id=?',
                    (company_id,)
                ).fetchone()[0]
            )

        return safe_int(
            c.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        )
    except Exception:
        return 0


def sum_column(c, table, column, company_id=None):
    if not table_exists(c, table):
        return 0.0

    cols = columns(c, table)

    if column not in cols:
        return 0.0

    try:
        if company_id is not None and "company_id" in cols:
            row = c.execute(
                f'''
                SELECT COALESCE(SUM("{column}"),0)
                FROM "{table}"
                WHERE company_id=?
                ''',
                (company_id,)
            ).fetchone()
        else:
            row = c.execute(
                f'''
                SELECT COALESCE(SUM("{column}"),0)
                FROM "{table}"
                '''
            ).fetchone()

        return safe_float(row[0] if row else 0)
    except Exception:
        return 0.0


def first_existing_column(c, table, candidates):
    cols = columns(c, table)

    for item in candidates:
        if item in cols:
            return item

    return None


# ============================================================
# TABLES MASTER
# ============================================================

def ensure_tables():

    c = db()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS master_company_config (
        company_id INTEGER PRIMARY KEY,
        plan TEXT DEFAULT 'standard',
        monthly_price REAL DEFAULT 0,
        billing_status TEXT DEFAULT 'active',
        billing_due_date TEXT,
        trial_until TEXT,
        max_users INTEGER DEFAULT 10,
        max_products INTEGER DEFAULT 1000,
        max_customers INTEGER DEFAULT 5000,
        max_sales_month INTEGER DEFAULT 10000,
        max_storage_mb INTEGER DEFAULT 1024,
        support_enabled INTEGER DEFAULT 1,
        notes TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_company_flags (
        company_id INTEGER NOT NULL,
        feature TEXT NOT NULL,
        enabled INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(company_id, feature)
    );

    CREATE TABLE IF NOT EXISTS master_support_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        master_user_id INTEGER,
        company_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        reason TEXT DEFAULT 'suporte',
        active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS master_admin_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        master_user_id INTEGER,
        action TEXT NOT NULL,
        company_id INTEGER,
        target_user_id INTEGER,
        details TEXT DEFAULT '',
        ip TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS master_billing_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        amount REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        reference TEXT DEFAULT '',
        due_date TEXT,
        paid_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        monthly_price REAL DEFAULT 0,
        max_users INTEGER DEFAULT 10,
        max_products INTEGER DEFAULT 1000,
        max_customers INTEGER DEFAULT 5000,
        max_sales_month INTEGER DEFAULT 10000,
        max_storage_mb INTEGER DEFAULT 1024,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_security_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        severity TEXT DEFAULT 'info',
        company_id INTEGER,
        user_id INTEGER,
        ip TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        details TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        last_used_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_webhooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        event TEXT DEFAULT '*',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_integrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        status TEXT DEFAULT 'disabled',
        config_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_maintenance (
        id INTEGER PRIMARY KEY CHECK (id=1),
        enabled INTEGER DEFAULT 0,
        message TEXT DEFAULT 'Sistema em manutenção.',
        allow_master INTEGER DEFAULT 1,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS master_platform_settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_master_actions_company
        ON master_admin_actions(company_id);

    CREATE INDEX IF NOT EXISTS idx_master_actions_created
        ON master_admin_actions(created_at);

    CREATE INDEX IF NOT EXISTS idx_master_security_created
        ON master_security_events(created_at);

    CREATE INDEX IF NOT EXISTS idx_master_support_company
        ON master_support_sessions(company_id);

    CREATE INDEX IF NOT EXISTS idx_master_billing_company
        ON master_billing_history(company_id);

    CREATE INDEX IF NOT EXISTS idx_master_webhooks_company
        ON master_webhooks(company_id);

    CREATE INDEX IF NOT EXISTS idx_master_integrations_company
        ON master_integrations(company_id);
    """)

    # master_plans pode ter sido criada antes por outro módulo com menos colunas.
    plan_cols = {r[1] for r in c.execute("PRAGMA table_info(master_plans)").fetchall()}
    for col, ddl in (
        ("description", "TEXT DEFAULT ''"),
        ("max_customers", "INTEGER DEFAULT 5000"),
        ("max_sales_month", "INTEGER DEFAULT 10000"),
        ("max_storage_mb", "INTEGER DEFAULT 1024"),
    ):
        if col not in plan_cols:
            c.execute(f"ALTER TABLE master_plans ADD COLUMN {col} {ddl}")

    defaults = [
        ("standard", 0, 10, 1000, 5000, 10000, 1024),
        ("pro", 149, 25, 5000, 20000, 50000, 5120),
        ("premium", 299, 50, 15000, 50000, 150000, 20480),
        ("enterprise", 599, 9999, 999999, 999999, 999999, 102400),
    ]

    for item in defaults:
        c.execute("""
            INSERT INTO master_plans
            (
                name,
                monthly_price,
                max_users,
                max_products,
                max_customers,
                max_sales_month,
                max_storage_mb
            )
            SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM master_plans WHERE name = ?
            )
        """, (*item, item[0]))

    c.execute("""
        INSERT OR IGNORE INTO master_maintenance
        (id, enabled, message, allow_master)
        VALUES (1, 0, 'Sistema em manutenção.', 1)
    """)

    settings = {
        "platform_name": "SaaS Gestão Empresarial",
        "platform_environment": "production",
        "platform_version": "1.0.0",
        "session_limit": "5",
        "backup_retention_days": "30",
        "support_enabled": "1",
    }

    for key, value in settings.items():
        c.execute("""
            INSERT OR IGNORE INTO master_platform_settings
            (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, now()))

    if table_exists(c, "companies"):
        company_rows = c.execute(
            "SELECT id FROM companies"
        ).fetchall()

        for row in company_rows:
            cid = row["id"]

            c.execute("""
                INSERT OR IGNORE INTO master_company_config
                (company_id)
                VALUES (?)
            """, (cid,))

            c.execute("""
                INSERT OR IGNORE INTO master_company_flags
                (company_id, feature, enabled)
                VALUES
                (?, 'sales', 1),
                (?, 'stock', 1),
                (?, 'purchases', 1),
                (?, 'customers', 1),
                (?, 'reports', 1),
                (?, 'whatsapp', 0),
                (?, 'nfe', 0),
                (?, 'payments', 0)
            """, (
                cid, cid, cid, cid,
                cid, cid, cid, cid
            ))

    c.commit()
    c.close()


# ============================================================
# AUTH MASTER
# ============================================================

def get_master(request, c):

    token = request.cookies.get("session")

    if not token or not table_exists(c, "sessions"):
        return None

    try:
        row = c.execute("""
            SELECT
                u.id,
                u.name,
                u.username,
                u.role,
                u.active,
                u.company_id
            FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token=?
            LIMIT 1
        """, (token,)).fetchone()
    except Exception:
        return None

    if not row:
        return None

    if not row["active"]:
        return None

    if str(row["role"]).lower() != "master":
        return None

    return dict(row)


def guard(request):

    c = db()

    try:
        user = get_master(request, c)

        if not user:
            c.close()
            return None, None

        return user, c

    except Exception:
        c.close()
        return None, None


# ============================================================
# AUDITORIA
# ============================================================

def record_action(
    c,
    master_id,
    action,
    company_id=None,
    user_id=None,
    details="",
    request=None
):

    ip = ""

    if request:
        try:
            ip = request.client.host if request.client else ""
        except Exception:
            ip = ""

    c.execute("""
        INSERT INTO master_admin_actions
        (
            master_user_id,
            action,
            company_id,
            target_user_id,
            details,
            ip,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        master_id,
        str(action)[:200],
        company_id,
        user_id,
        str(details)[:5000],
        ip,
        now()
    ))


def security_event(
    c,
    event_type,
    severity="info",
    company_id=None,
    user_id=None,
    details="",
    request=None
):

    ip = ""
    ua = ""

    if request:
        try:
            ip = request.client.host if request.client else ""
        except Exception:
            pass

        try:
            ua = request.headers.get("user-agent", "")
        except Exception:
            pass

    c.execute("""
        INSERT INTO master_security_events
        (
            event_type,
            severity,
            company_id,
            user_id,
            ip,
            user_agent,
            details,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_type,
        severity,
        company_id,
        user_id,
        ip[:200],
        ua[:1000],
        str(details)[:5000],
        now()
    ))


# ============================================================
# EMPRESA
# ============================================================

def ensure_company(c, company_id):

    c.execute("""
        INSERT OR IGNORE INTO master_company_config
        (company_id)
        VALUES (?)
    """, (company_id,))


def company_snapshot(c, company_id):

    if not table_exists(c, "companies"):
        return None

    company = c.execute(
        "SELECT * FROM companies WHERE id=?",
        (company_id,)
    ).fetchone()

    if not company:
        return None

    ensure_company(c, company_id)

    config = c.execute("""
        SELECT *
        FROM master_company_config
        WHERE company_id=?
    """, (company_id,)).fetchone()

    users = count_rows(c, "users", company_id)
    products = count_rows(c, "products", company_id)
    customers = count_rows(c, "customers", company_id)
    sales = count_rows(c, "sales", company_id)
    purchases = count_rows(c, "purchases", company_id)
    expenses = count_rows(c, "expenses", company_id)
    stock = count_rows(c, "stock_movements", company_id)

    revenue = 0

    if table_exists(c, "sales"):

        amount_col = first_existing_column(
            c,
            "sales",
            [
                "total",
                "total_amount",
                "amount",
                "valor",
                "value"
            ]
        )

        if amount_col:
            revenue = sum_column(
                c,
                "sales",
                amount_col,
                company_id
            )

    return {
        "company": dict(company),
        "config": dict(config) if config else {},
        "usage": {
            "users": users,
            "products": products,
            "customers": customers,
            "sales": sales,
            "purchases": purchases,
            "expenses": expenses,
            "stock_movements": stock,
            "revenue": revenue
        }
    }


# ============================================================
# REGISTER
# ============================================================

def register_master_plus(app):

    ensure_tables()

    from fastapi import Request
    from fastapi.responses import JSONResponse, HTMLResponse

    def denied():
        return JSONResponse(
            {
                "ok": False,
                "error": "Acesso restrito ao Master"
            },
            status_code=403
        )

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    @app.get("/master/plus")
    async def master_plus_page(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        c.close()

        return HTMLResponse(MASTER_HTML)

    @app.get("/master/plus/")
    async def master_plus_page_slash(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        c.close()

        return HTMLResponse(MASTER_HTML)

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

    @app.get("/master/plus/api/dashboard")
    async def master_dashboard(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        companies = count_rows(c, "companies")
        users = count_rows(c, "users")
        products = count_rows(c, "products")
        customers = count_rows(c, "customers")
        sales = count_rows(c, "sales")
        purchases = count_rows(c, "purchases")
        expenses = count_rows(c, "expenses")

        revenue = 0
        expense_total = 0

        if table_exists(c, "sales"):
            col = first_existing_column(
                c,
                "sales",
                ["total", "total_amount", "amount", "valor", "value"]
            )
            if col:
                revenue = sum_column(c, "sales", col)

        if table_exists(c, "expenses"):
            col = first_existing_column(
                c,
                "expenses",
                ["amount", "value", "valor", "total"]
            )
            if col:
                expense_total = sum_column(c, "expenses", col)

        online = 0

        if table_exists(c, "sessions"):
            try:
                online = c.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
            except Exception:
                online = 0

        db_size = 0

        try:
            db_size = DB_PATH.stat().st_size
        except Exception:
            pass

        c.close()

        return {
            "ok": True,
            "dashboard": {
                "companies": companies,
                "users": users,
                "products": products,
                "customers": customers,
                "sales": sales,
                "purchases": purchases,
                "expenses": expenses,
                "online_sessions": online,
                "revenue": revenue,
                "expenses_value": expense_total,
                "result": revenue - expense_total,
                "database_mb": round(
                    db_size / 1024 / 1024,
                    2
                )
            }
        }

    # --------------------------------------------------------
    # COMPANIES
    # --------------------------------------------------------

    @app.get("/master/plus/api/companies")
    async def master_companies(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT *
            FROM companies
            ORDER BY id DESC
        """).fetchall()

        result = []

        for row in rows:

            cid = row["id"]

            ensure_company(c, cid)

            config = c.execute("""
                SELECT *
                FROM master_company_config
                WHERE company_id=?
            """, (cid,)).fetchone()

            item = dict(row)

            item["users"] = count_rows(
                c,
                "users",
                cid
            )

            item["products"] = count_rows(
                c,
                "products",
                cid
            )

            item["customers"] = count_rows(
                c,
                "customers",
                cid
            )

            item["sales"] = count_rows(
                c,
                "sales",
                cid
            )

            item["config"] = (
                dict(config)
                if config
                else {}
            )

            result.append(item)

        c.commit()
        c.close()

        return {
            "ok": True,
            "companies": result
        }

    @app.get("/master/plus/api/company/{company_id}")
    async def master_company(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        data = company_snapshot(
            c,
            company_id
        )

        if not data:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Empresa não encontrada"
                },
                status_code=404
            )

        users = []

        if table_exists(c, "users"):

            rows = c.execute("""
                SELECT
                    id,
                    name,
                    username,
                    role,
                    active,
                    company_id,
                    created_at
                FROM users
                WHERE company_id=?
                ORDER BY id
            """, (company_id,)).fetchall()

            users = [
                dict(row)
                for row in rows
            ]

        flags = c.execute("""
            SELECT feature, enabled
            FROM master_company_flags
            WHERE company_id=?
            ORDER BY feature
        """, (company_id,)).fetchall()

        c.commit()
        c.close()

        data["users"] = users

        data["flags"] = [
            dict(row)
            for row in flags
        ]

        return {
            "ok": True,
            "data": data
        }

    # --------------------------------------------------------
    # CREATE COMPANY
    # --------------------------------------------------------

    @app.post("/master/plus/api/company/create")
    async def master_company_create(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        name = str(
            body.get("name", "")
        ).strip()
        segment = str(body.get("segment", "LOJA") or "").strip().upper()

        if segment not in {"LOJA", "RESTAURANTE"}:
            c.close()
            return JSONResponse(
                {"ok": False, "error": "Segmento deve ser LOJA ou RESTAURANTE"},
                status_code=400,
            )

        if not name:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Nome da empresa é obrigatório"
                },
                status_code=400
            )

        if not table_exists(c, "companies"):
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Tabela companies não existe"
                },
                status_code=500
            )

        cols = columns(c, "companies")

        insert_cols = []
        insert_vals = []

        if "name" in cols:
            insert_cols.append("name")
            insert_vals.append(name)

        if "active" in cols:
            insert_cols.append("active")
            insert_vals.append(1)

        if "segment" in cols:
            insert_cols.append("segment")
            insert_vals.append(segment)


        
        if "created_at" in cols:
            insert_cols.append("created_at")
            insert_vals.append(datetime.now().isoformat(timespec="seconds"))
        if not insert_cols:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Estrutura de companies incompatível"
                },
                status_code=500
            )

        placeholders = ",".join(
            ["?"] * len(insert_vals)
        )

        sql = f'''
            INSERT INTO companies
            ({",".join(insert_cols)})
            VALUES ({placeholders})
        '''

        cur = c.execute(
            sql,
            tuple(insert_vals)
        )

        company_id = cur.lastrowid

        ensure_company(c, company_id)

        for feature, enabled in [
            ("sales", 1),
            ("stock", 1),
            ("purchases", 1),
            ("customers", 1),
            ("reports", 1),
            ("whatsapp", 0),
            ("nfe", 0),
            ("payments", 0),
        ]:
            c.execute("""
                INSERT OR IGNORE INTO master_company_flags
                (company_id, feature, enabled)
                VALUES (?, ?, ?)
            """, (
                company_id,
                feature,
                enabled
            ))

        record_action(
            c,
            user["id"],
            "COMPANY_CREATE",
            company_id,
            details=f"Empresa criada: {name}; segmento={segment}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "company_id": company_id,
            "message": "Empresa criada."
        }

    # --------------------------------------------------------
    # SUSPEND / ACTIVATE
    # --------------------------------------------------------

    @app.post("/master/plus/api/company/{company_id}/suspend")
    async def master_suspend(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        if not table_exists(c, "companies"):
            c.close()
            return denied()

        cols = columns(c, "companies")

        if "active" in cols:
            c.execute(
                "UPDATE companies SET active=0 WHERE id=?",
                (company_id,)
            )

        ensure_company(c, company_id)

        c.execute("""
            UPDATE master_company_config
            SET
                billing_status='suspended',
                updated_at=?
            WHERE company_id=?
        """, (
            now(),
            company_id
        ))

        if table_exists(c, "sessions"):

            users = c.execute("""
                SELECT id
                FROM users
                WHERE company_id=?
            """, (company_id,)).fetchall()

            for row in users:
                c.execute(
                    "DELETE FROM sessions WHERE user_id=?",
                    (row["id"],)
                )

        record_action(
            c,
            user["id"],
            "COMPANY_SUSPEND",
            company_id,
            details="Empresa suspensa e sessões encerradas.",
            request=request
        )

        security_event(
            c,
            "company_suspended",
            "warning",
            company_id=company_id,
            details="Empresa suspensa pelo Master.",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "message": "Empresa suspensa."
        }

    @app.post("/master/plus/api/company/{company_id}/activate")
    async def master_activate(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        cols = columns(c, "companies")

        if "active" in cols:
            c.execute(
                "UPDATE companies SET active=1 WHERE id=?",
                (company_id,)
            )

        ensure_company(c, company_id)

        c.execute("""
            UPDATE master_company_config
            SET
                billing_status='active',
                updated_at=?
            WHERE company_id=?
        """, (
            now(),
            company_id
        ))

        record_action(
            c,
            user["id"],
            "COMPANY_ACTIVATE",
            company_id,
            details="Empresa reativada.",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "message": "Empresa reativada."
        }

    # --------------------------------------------------------
    # COMPANY PLAN
    # --------------------------------------------------------

    @app.post("/master/plus/api/company/{company_id}/plan")
    async def master_plan(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        plan = str(
            body.get("plan", "standard")
        ).strip()[:50]

        price = safe_float(
            body.get("monthly_price", 0)
        )

        status = str(
            body.get("billing_status", "active")
        ).strip()[:50]

        due = body.get(
            "billing_due_date"
        )

        trial = body.get(
            "trial_until"
        )

        ensure_company(c, company_id)

        c.execute("""
            UPDATE master_company_config
            SET
                plan=?,
                monthly_price=?,
                billing_status=?,
                billing_due_date=?,
                trial_until=?,
                updated_at=?
            WHERE company_id=?
        """, (
            plan,
            price,
            status,
            due,
            trial,
            now(),
            company_id
        ))

        record_action(
            c,
            user["id"],
            "PLAN_UPDATE",
            company_id,
            details=json.dumps(
                body,
                ensure_ascii=False
            ),
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "message": "Plano atualizado."
        }

    # --------------------------------------------------------
    # LIMITS
    # --------------------------------------------------------

    @app.post("/master/plus/api/company/{company_id}/limits")
    async def master_limits(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        ensure_company(c, company_id)

        fields = [
            "max_users",
            "max_products",
            "max_customers",
            "max_sales_month",
            "max_storage_mb"
        ]

        values = {}

        for field in fields:

            if field in body:

                values[field] = max(
                    0,
                    safe_int(body[field])
                )

        if values:

            set_sql = ", ".join(
                f"{field}=?"
                for field in values
            )

            params = list(
                values.values()
            )

            params.extend([
                now(),
                company_id
            ])

            c.execute(
                f"""
                UPDATE master_company_config
                SET
                    {set_sql},
                    updated_at=?
                WHERE company_id=?
                """,
                tuple(params)
            )

        record_action(
            c,
            user["id"],
            "LIMITS_UPDATE",
            company_id,
            details=json.dumps(
                values,
                ensure_ascii=False
            ),
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "message": "Limites atualizados."
        }

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    @app.get("/master/plus/api/users")
    async def master_users(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        if not table_exists(c, "users"):
            c.close()
            return {
                "ok": True,
                "users": []
            }

        rows = c.execute("""
            SELECT
                u.id,
                u.name,
                u.username,
                u.role,
                u.active,
                u.company_id,
                c.name AS company_name,
                u.created_at
            FROM users u
            LEFT JOIN companies c
                ON c.id=u.company_id
            ORDER BY u.id DESC
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "users": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/user/{user_id}/toggle")
    async def master_user_toggle(
        request: Request,
        user_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        row = c.execute("""
            SELECT id, role, active, company_id
            FROM users
            WHERE id=?
        """, (user_id,)).fetchone()

        if not row:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Usuário não encontrado"
                },
                status_code=404
            )

        if str(row["role"]).lower() == "master":
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "O usuário Master é protegido."
                },
                status_code=400
            )

        new_value = 0 if row["active"] else 1

        c.execute(
            "UPDATE users SET active=? WHERE id=?",
            (new_value, user_id)
        )

        record_action(
            c,
            user["id"],
            "USER_TOGGLE",
            row["company_id"],
            user_id,
            details=f"active={new_value}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "active": bool(new_value)
        }

    @app.post("/master/plus/api/user/{user_id}/role")
    async def master_user_role(
        request: Request,
        user_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        role = str(
            body.get("role", "")
        ).lower().strip()

        allowed = {
            "admin",
            "gerente",
            "vendedor",
            "caixa"
        }

        if role not in allowed:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Cargo inválido."
                },
                status_code=400
            )

        row = c.execute("""
            SELECT role, company_id
            FROM users
            WHERE id=?
        """, (user_id,)).fetchone()

        if not row:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Usuário não encontrado."
                },
                status_code=404
            )

        if str(row["role"]).lower() == "master":
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "O Master não pode ser rebaixado."
                },
                status_code=400
            )

        c.execute(
            "UPDATE users SET role=? WHERE id=?",
            (role, user_id)
        )

        record_action(
            c,
            user["id"],
            "USER_ROLE_CHANGE",
            row["company_id"],
            user_id,
            details=f"Novo cargo: {role}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "role": role
        }

    @app.post("/master/plus/api/user/{user_id}/logout")
    async def master_user_logout(
        request: Request,
        user_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        if table_exists(c, "sessions"):

            c.execute(
                "DELETE FROM sessions WHERE user_id=?",
                (user_id,)
            )

        row = c.execute("""
            SELECT company_id, role
            FROM users
            WHERE id=?
        """, (user_id,)).fetchone()

        company_id = (
            row["company_id"]
            if row
            else None
        )

        record_action(
            c,
            user["id"],
            "USER_FORCE_LOGOUT",
            company_id,
            user_id,
            details="Sessões encerradas.",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "message": "Sessões encerradas."
        }

    # --------------------------------------------------------
    # SESSIONS
    # --------------------------------------------------------

    @app.get("/master/plus/api/sessions")
    async def master_sessions(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        if not table_exists(c, "sessions"):
            c.close()

            return {
                "ok": True,
                "sessions": []
            }

        rows = c.execute("""
            SELECT
                s.*,
                u.name,
                u.username,
                u.role,
                u.company_id,
                c.name AS company_name
            FROM sessions s
            JOIN users u ON u.id=s.user_id
            LEFT JOIN companies c
                ON c.id=u.company_id
            ORDER BY s.created_at DESC
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "sessions": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/sessions/logout-all")
    async def master_logout_all(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        total = 0

        if table_exists(c, "sessions"):

            rows = c.execute(
                "SELECT id FROM users WHERE role!='master'"
            ).fetchall()

            for row in rows:

                cur = c.execute(
                    "DELETE FROM sessions WHERE user_id=?",
                    (row["id"],)
                )

                total += cur.rowcount

        record_action(
            c,
            user["id"],
            "GLOBAL_LOGOUT",
            details=f"{total} sessões encerradas.",
            request=request
        )

        security_event(
            c,
            "global_logout",
            "warning",
            details=f"{total} sessões encerradas pelo Master.",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "closed_sessions": total
        }

    # --------------------------------------------------------
    # SUPPORT MODE
    # --------------------------------------------------------

    @app.post("/master/plus/api/support/start/{company_id}")
    async def support_start(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        company = c.execute("""
            SELECT id, name
            FROM companies
            WHERE id=?
        """, (company_id,)).fetchone()

        if not company:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Empresa não encontrada."
                },
                status_code=404
            )

        body = await request.json()

        reason = str(
            body.get(
                "reason",
                "suporte"
            )
        )[:500]

        token = secrets.token_urlsafe(32)

        c.execute("""
            UPDATE master_support_sessions
            SET
                active=0,
                ended_at=?
            WHERE
                master_user_id=?
                AND active=1
        """, (
            now(),
            user["id"]
        ))

        c.execute("""
            INSERT INTO master_support_sessions
            (
                token,
                master_user_id,
                company_id,
                started_at,
                reason,
                active
            )
            VALUES (?, ?, ?, ?, ?, 1)
        """, (
            token,
            user["id"],
            company_id,
            now(),
            reason
        ))

        record_action(
            c,
            user["id"],
            "SUPPORT_START",
            company_id,
            details=f"Motivo: {reason}",
            request=request
        )

        security_event(
            c,
            "support_mode_start",
            "info",
            company_id=company_id,
            details=reason,
            request=request
        )

        c.commit()
        c.close()

        response = JSONResponse({
            "ok": True,
            "token": token,
            "company_id": company_id,
            "company_name": company["name"]
        })

        response.set_cookie(
            "master_support",
            token,
            httponly=True,
            samesite="lax",
            max_age=3600
        )

        return response

    @app.post("/master/plus/api/support/stop")
    async def support_stop(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        token = request.cookies.get(
            "master_support"
        )

        if token:

            c.execute("""
                UPDATE master_support_sessions
                SET
                    active=0,
                    ended_at=?
                WHERE
                    token=?
                    AND master_user_id=?
            """, (
                now(),
                token,
                user["id"]
            ))

        record_action(
            c,
            user["id"],
            "SUPPORT_STOP",
            details="Modo suporte encerrado.",
            request=request
        )

        c.commit()
        c.close()

        response = JSONResponse({
            "ok": True
        })

        response.delete_cookie(
            "master_support"
        )

        return response

    @app.get("/master/plus/api/support/current")
    async def support_current(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        token = request.cookies.get(
            "master_support"
        )

        if not token:
            c.close()

            return {
                "ok": True,
                "active": False
            }

        row = c.execute("""
            SELECT
                s.*,
                c.name AS company_name
            FROM master_support_sessions s
            JOIN companies c
                ON c.id=s.company_id
            WHERE
                s.token=?
                AND s.master_user_id=?
                AND s.active=1
            LIMIT 1
        """, (
            token,
            user["id"]
        )).fetchone()

        c.close()

        if not row:
            return {
                "ok": True,
                "active": False
            }

        return {
            "ok": True,
            "active": True,
            "support": dict(row)
        }

    @app.get("/master/plus/api/support/snapshot/{company_id}")
    async def support_snapshot(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        data = company_snapshot(
            c,
            company_id
        )

        if not data:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Empresa não encontrada."
                },
                status_code=404
            )

        recent_sales = []

        if table_exists(c, "sales"):

            cols = columns(c, "sales")

            order_col = first_existing_column(
                c,
                "sales",
                [
                    "created_at",
                    "date",
                    "data",
                    "id"
                ]
            )

            if order_col:

                select_cols = list(
                    cols.intersection({
                        "id",
                        "total",
                        "amount",
                        "total_amount",
                        "created_at",
                        "date",
                        "status"
                    })
                )

                if "id" not in select_cols:
                    select_cols.append("id")

                try:
                    rows = c.execute(
                        f'''
                        SELECT {",".join(
                            '"' + x + '"'
                            for x in select_cols
                        )}
                        FROM sales
                        WHERE company_id=?
                        ORDER BY "{order_col}" DESC
                        LIMIT 20
                        ''',
                        (company_id,)
                    ).fetchall()

                    recent_sales = [
                        dict(row)
                        for row in rows
                    ]
                except Exception:
                    recent_sales = []

        c.close()

        data["recent_sales"] = recent_sales

        return {
            "ok": True,
            "snapshot": data
        }

    # --------------------------------------------------------
    # BILLING
    # --------------------------------------------------------

    @app.get("/master/plus/api/billing")
    async def master_billing(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT
                c.id AS company_id,
                c.name AS company_name,
                m.plan,
                m.monthly_price,
                m.billing_status,
                m.billing_due_date,
                m.trial_until
            FROM companies c
            LEFT JOIN master_company_config m
                ON m.company_id=c.id
            ORDER BY c.id DESC
        """).fetchall()

        total_mrr = 0

        for row in rows:
            if row["billing_status"] == "active":
                total_mrr += safe_float(
                    row["monthly_price"]
                )

        history = c.execute("""
            SELECT *
            FROM master_billing_history
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "mrr": total_mrr,
            "companies": [
                dict(row)
                for row in rows
            ],
            "history": [
                dict(row)
                for row in history
            ]
        }

    @app.post("/master/plus/api/billing/history")
    async def master_billing_history(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        company_id = safe_int(
            body.get("company_id")
        )

        amount = safe_float(
            body.get("amount")
        )

        status = str(
            body.get("status", "pending")
        )[:30]

        reference = str(
            body.get("reference", "")
        )[:200]

        due_date = body.get(
            "due_date"
        )

        paid_at = body.get(
            "paid_at"
        )

        c.execute("""
            INSERT INTO master_billing_history
            (
                company_id,
                amount,
                status,
                reference,
                due_date,
                paid_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            company_id,
            amount,
            status,
            reference,
            due_date,
            paid_at
        ))

        record_action(
            c,
            user["id"],
            "BILLING_REGISTER",
            company_id,
            details=json.dumps(
                body,
                ensure_ascii=False
            ),
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True
        }

    # --------------------------------------------------------
    # ANALYTICS
    # --------------------------------------------------------

    @app.get("/master/plus/api/analytics")
    async def master_analytics(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        companies = []

        if table_exists(c, "companies"):

            rows = c.execute("""
                SELECT id, name
                FROM companies
                ORDER BY id
            """).fetchall()

            for row in rows:

                cid = row["id"]

                snapshot = company_snapshot(
                    c,
                    cid
                )

                if snapshot:

                    companies.append({
                        "id": cid,
                        "name": row["name"],
                        "users": snapshot["usage"]["users"],
                        "products": snapshot["usage"]["products"],
                        "customers": snapshot["usage"]["customers"],
                        "sales": snapshot["usage"]["sales"],
                        "revenue": snapshot["usage"]["revenue"]
                    })

        active = 0
        inactive = 0

        for company in companies:

            if company["sales"] > 0:
                active += 1
            else:
                inactive += 1

        c.close()

        return {
            "ok": True,
            "analytics": {
                "total_companies": len(companies),
                "active_companies": active,
                "inactive_companies": inactive,
                "companies": companies
            }
        }

    # --------------------------------------------------------
    # AUDIT
    # --------------------------------------------------------

    @app.get("/master/plus/api/audit")
    async def master_audit(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT *
            FROM master_admin_actions
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "audit": [
                dict(row)
                for row in rows
            ]
        }

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    @app.get("/master/plus/api/security")
    async def master_security(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT *
            FROM master_security_events
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "events": [
                dict(row)
                for row in rows
            ]
        }

    # --------------------------------------------------------
    # ALERTS
    # --------------------------------------------------------

    @app.get("/master/plus/api/alerts")
    async def master_alerts(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        alerts = []

        if table_exists(c, "companies"):

            rows = c.execute("""
                SELECT id, name
                FROM companies
            """).fetchall()

            for row in rows:

                cid = row["id"]

                users = count_rows(
                    c,
                    "users",
                    cid
                )

                admins = 0

                if table_exists(c, "users"):

                    admins = safe_int(
                        c.execute("""
                            SELECT COUNT(*)
                            FROM users
                            WHERE company_id=?
                            AND role='admin'
                            AND active=1
                        """, (cid,)).fetchone()[0]
                    )

                if users > 0 and admins == 0:

                    alerts.append({
                        "severity": "critical",
                        "type": "company_without_admin",
                        "company_id": cid,
                        "company": row["name"],
                        "message": "Empresa sem administrador ativo."
                    })

                config = c.execute("""
                    SELECT *
                    FROM master_company_config
                    WHERE company_id=?
                """, (cid,)).fetchone()

                if config:

                    if config["billing_status"] in {
                        "overdue",
                        "suspended"
                    }:

                        alerts.append({
                            "severity": "warning",
                            "type": "billing",
                            "company_id": cid,
                            "company": row["name"],
                            "message":
                                "Empresa com status financeiro: "
                                + str(
                                    config["billing_status"]
                                )
                        })

        try:

            db_size = DB_PATH.stat().st_size

            if db_size > 1024 * 1024 * 1024:

                alerts.append({
                    "severity": "warning",
                    "type": "database_size",
                    "message":
                        "Banco de dados acima de 1 GB."
                })

        except Exception:
            pass

        c.close()

        return {
            "ok": True,
            "alerts": alerts
        }

    # --------------------------------------------------------
    # FEATURE FLAGS
    # --------------------------------------------------------

    @app.get("/master/plus/api/flags/{company_id}")
    async def master_flags(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT feature, enabled
            FROM master_company_flags
            WHERE company_id=?
            ORDER BY feature
        """, (company_id,)).fetchall()

        c.close()

        return {
            "ok": True,
            "flags": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/flag/{company_id}")
    async def master_flag(
        request: Request,
        company_id: int
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        feature = str(
            body.get("feature", "")
        ).strip()[:100]

        enabled = (
            1
            if bool(body.get("enabled"))
            else 0
        )

        if not feature:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Feature obrigatória."
                },
                status_code=400
            )

        c.execute("""
            INSERT INTO master_company_flags
            (
                company_id,
                feature,
                enabled,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(company_id, feature)
            DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
        """, (
            company_id,
            feature,
            enabled,
            now()
        ))

        record_action(
            c,
            user["id"],
            "FEATURE_FLAG",
            company_id,
            details=f"{feature}={'ON' if enabled else 'OFF'}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "feature": feature,
            "enabled": bool(enabled)
        }

    # --------------------------------------------------------
    # BACKUPS
    # --------------------------------------------------------

    @app.get("/master/plus/api/backups")
    async def master_backups(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        BACKUP_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        result = []

        for item in sorted(
            BACKUP_DIR.iterdir(),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        ):

            if item.is_file():

                try:

                    stat = item.stat()

                    result.append({
                        "name": item.name,
                        "size": stat.st_size,
                        "size_mb": round(
                            stat.st_size /
                            1024 /
                            1024,
                            2
                        ),
                        "modified":
                            datetime.fromtimestamp(
                                stat.st_mtime
                            ).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                    })

                except Exception:
                    pass

        c.close()

        return {
            "ok": True,
            "backups": result[:300]
        }

    @app.post("/master/plus/api/backup")
    async def master_backup(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        BACKUP_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        stamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        destination = (
            BACKUP_DIR /
            f"loja_db_master_{stamp}.db"
        )

        c.commit()
        c.close()

        shutil.copy2(
            DB_PATH,
            destination
        )

        c2 = db()

        record_action(
            c2,
            user["id"],
            "BACKUP_CREATE",
            details=str(destination),
            request=request
        )

        c2.commit()
        c2.close()

        return {
            "ok": True,
            "file": destination.name,
            "size_mb": round(
                destination.stat().st_size /
                1024 /
                1024,
                2
            )
        }

    # --------------------------------------------------------
    # DIAGNOSTICS
    # --------------------------------------------------------

    @app.get("/master/plus/api/diagnostics")
    async def master_diagnostics(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        result = {
            "database": False,
            "integrity": "unknown",
            "tables": {},
            "orphan_users": 0,
            "companies_without_admin": 0,
            "invalid_sessions": 0
        }

        try:

            c.execute("SELECT 1")

            result["database"] = True

            integrity = c.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

            result["integrity"] = integrity

        except Exception as exc:

            result["integrity"] = str(exc)

        required_tables = [
            "companies",
            "users",
            "products",
            "customers",
            "sales",
            "expenses",
            "sessions",
            "audit_log"
        ]

        for table in required_tables:

            result["tables"][table] = {
                "exists": table_exists(c, table),
                "rows": count_rows(c, table)
            }

        if table_exists(c, "users"):

            try:

                result["orphan_users"] = safe_int(
                    c.execute("""
                        SELECT COUNT(*)
                        FROM users
                        WHERE
                            role!='master'
                            AND (
                                company_id IS NULL
                                OR company_id NOT IN (
                                    SELECT id
                                    FROM companies
                                )
                            )
                    """).fetchone()[0]
                )

            except Exception:
                pass

        if table_exists(c, "companies"):

            try:

                result["companies_without_admin"] = safe_int(
                    c.execute("""
                        SELECT COUNT(*)
                        FROM companies c
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM users u
                            WHERE
                                u.company_id=c.id
                                AND u.role='admin'
                                AND u.active=1
                        )
                    """).fetchone()[0]
                )

            except Exception:
                pass

        if table_exists(c, "sessions"):

            try:

                result["invalid_sessions"] = safe_int(
                    c.execute("""
                        SELECT COUNT(*)
                        FROM sessions s
                        LEFT JOIN users u
                            ON u.id=s.user_id
                        WHERE
                            u.id IS NULL
                            OR u.active=0
                    """).fetchone()[0]
                )

            except Exception:
                pass

        c.close()

        return {
            "ok": True,
            "diagnostics": result
        }

    # --------------------------------------------------------
    # DATA CENTER
    # --------------------------------------------------------

    @app.get("/master/plus/api/data")
    async def master_data(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT
                name,
                type
            FROM sqlite_master
            WHERE type IN ('table','index')
            ORDER BY type, name
        """).fetchall()

        tables = []

        for row in rows:

            item = dict(row)

            if item["type"] == "table":
                item["rows"] = count_rows(
                    c,
                    item["name"]
                )

            tables.append(item)

        c.close()

        return {
            "ok": True,
            "objects": tables
        }

    # --------------------------------------------------------
    # SYSTEM
    # --------------------------------------------------------

    @app.get("/master/plus/api/system")
    async def master_system(request: Request):

        user, c = guard(request)

        if not user:
            return denied()

        try:
            db_size = DB_PATH.stat().st_size
        except Exception:
            db_size = 0

        disk = shutil.disk_usage(
            str(BASE_DIR)
        )

        c.close()

        return {
            "ok": True,
            "system": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "pid": os.getpid(),
                "cwd": str(BASE_DIR),
                "database_mb": round(
                    db_size / 1024 / 1024,
                    2
                ),
                "disk_total_gb": round(
                    disk.total / 1024 / 1024 / 1024,
                    2
                ),
                "disk_free_gb": round(
                    disk.free / 1024 / 1024 / 1024,
                    2
                ),
                "disk_used_gb": round(
                    disk.used / 1024 / 1024 / 1024,
                    2
                )
            }
        }

    # --------------------------------------------------------
    # MAINTENANCE
    # --------------------------------------------------------

    @app.get("/master/plus/api/maintenance")
    async def master_maintenance_get(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        row = c.execute("""
            SELECT *
            FROM master_maintenance
            WHERE id=1
        """).fetchone()

        c.close()

        return {
            "ok": True,
            "maintenance":
                dict(row)
                if row
                else {}
        }

    @app.post("/master/plus/api/maintenance")
    async def master_maintenance_set(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        enabled = (
            1
            if bool(body.get("enabled"))
            else 0
        )

        message = str(
            body.get(
                "message",
                "Sistema em manutenção."
            )
        )[:1000]

        allow_master = (
            1
            if body.get(
                "allow_master",
                True
            )
            else 0
        )

        c.execute("""
            UPDATE master_maintenance
            SET
                enabled=?,
                message=?,
                allow_master=?,
                updated_at=?
            WHERE id=1
        """, (
            enabled,
            message,
            allow_master,
            now()
        ))

        record_action(
            c,
            user["id"],
            "MAINTENANCE_CHANGE",
            details=json.dumps(
                {
                    "enabled": enabled,
                    "message": message,
                    "allow_master": allow_master
                },
                ensure_ascii=False
            ),
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "enabled": bool(enabled)
        }

    # --------------------------------------------------------
    # PLATFORM SETTINGS
    # --------------------------------------------------------

    @app.get("/master/plus/api/settings")
    async def master_settings_get(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT key, value, updated_at
            FROM master_platform_settings
            ORDER BY key
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "settings": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/settings")
    async def master_settings_set(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        for key, value in body.items():

            key = str(key)[:100]
            value = str(value)[:2000]

            c.execute("""
                INSERT INTO master_platform_settings
                (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key)
                DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
            """, (
                key,
                value,
                now()
            ))

        record_action(
            c,
            user["id"],
            "PLATFORM_SETTINGS_UPDATE",
            details=json.dumps(
                body,
                ensure_ascii=False
            ),
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True
        }

    # --------------------------------------------------------
    # API KEYS
    # --------------------------------------------------------

    @app.get("/master/plus/api/integrations/keys")
    async def master_api_keys(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT
                id,
                company_id,
                name,
                key_prefix,
                active,
                last_used_at,
                created_at
            FROM master_api_keys
            ORDER BY id DESC
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "keys": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/integrations/key")
    async def master_create_key(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        company_id = safe_int(
            body.get("company_id")
        )

        name = str(
            body.get(
                "name",
                "API Key"
            )
        )[:100]

        raw = (
            "sk_"
            + secrets.token_urlsafe(32)
        )

        key_hash = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        key_prefix = raw[:12]

        c.execute("""
            INSERT INTO master_api_keys
            (
                company_id,
                name,
                key_hash,
                key_prefix
            )
            VALUES (?, ?, ?, ?)
        """, (
            company_id,
            name,
            key_hash,
            key_prefix
        ))

        record_action(
            c,
            user["id"],
            "API_KEY_CREATE",
            company_id,
            details=f"API Key criada: {name}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True,
            "key": raw,
            "warning":
                "A chave completa é exibida somente agora."
        }

    # --------------------------------------------------------
    # WEBHOOKS
    # --------------------------------------------------------

    @app.get("/master/plus/api/integrations/webhooks")
    async def master_webhooks(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT *
            FROM master_webhooks
            ORDER BY id DESC
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "webhooks": [
                dict(row)
                for row in rows
            ]
        }

    @app.post("/master/plus/api/integrations/webhook")
    async def master_create_webhook(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        body = await request.json()

        company_id = safe_int(
            body.get("company_id")
        )

        name = str(
            body.get(
                "name",
                "Webhook"
            )
        )[:100]

        url = str(
            body.get(
                "url",
                ""
            )
        )[:2000]

        event = str(
            body.get(
                "event",
                "*"
            )
        )[:100]

        if not url:
            c.close()

            return JSONResponse(
                {
                    "ok": False,
                    "error": "URL obrigatória."
                },
                status_code=400
            )

        c.execute("""
            INSERT INTO master_webhooks
            (
                company_id,
                name,
                url,
                event
            )
            VALUES (?, ?, ?, ?)
        """, (
            company_id,
            name,
            url,
            event
        ))

        record_action(
            c,
            user["id"],
            "WEBHOOK_CREATE",
            company_id,
            details=f"{name} / {event}",
            request=request
        )

        c.commit()
        c.close()

        return {
            "ok": True
        }

    # --------------------------------------------------------
    # INTEGRATIONS
    # --------------------------------------------------------

    @app.get("/master/plus/api/integrations")
    async def master_integrations(
        request: Request
    ):

        user, c = guard(request)

        if not user:
            return denied()

        rows = c.execute("""
            SELECT *
            FROM master_integrations
            ORDER BY id DESC
        """).fetchall()

        c.close()

        return {
            "ok": True,
            "integrations": [
                dict(row)
                for row in rows
            ]
        }

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    @app.get("/master/plus/api/search")
    async def master_search(
        request: Request,
        q: str = ""
    ):

        user, c = guard(request)

        if not user:
            return denied()

        q = str(q).strip()[:100]

        if not q:
            c.close()

            return {
                "ok": True,
                "results": []
            }

        results = []

        if table_exists(c, "companies"):

            cols = columns(c, "companies")

            if "name" in cols:

                rows = c.execute("""
                    SELECT id, name
                    FROM companies
                    WHERE name LIKE ?
                    LIMIT 20
                """, (
                    f"%{q}%",
                )).fetchall()

                for row in rows:

                    results.append({
                        "type": "company",
                        "id": row["id"],
                        "name": row["name"]
                    })

        if table_exists(c, "users"):

            rows = c.execute("""
                SELECT
                    id,
                    name,
                    username,
                    role,
                    company_id
                FROM users
                WHERE
                    name LIKE ?
                    OR username LIKE ?
                LIMIT 30
            """, (
                f"%{q}%",
                f"%{q}%"
            )).fetchall()

            for row in rows:

                results.append({
                    "type": "user",
                    **dict(row)
                })

        c.close()

        return {
            "ok": True,
            "results": results
        }

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    print(
        "[MASTER PLUS] camada completa registrada."
    )


# ============================================================
# HTML
# ============================================================

MASTER_HTML = r"""
<!doctype html>
<html lang="pt-BR">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<title>Master Control Center</title>

<style>

:root{
--bg:#090b0e;
--bg2:#0e1115;
--panel:#12161b;
--panel2:#171c22;
--border:#27303a;
--border2:#34404c;
--text:#eef2f6;
--muted:#8995a3;
--accent:#65b9ff;
--green:#55d79c;
--red:#ff6979;
--yellow:#f2c75c;
}

*{
box-sizing:border-box;
}

body{
margin:0;
background:
radial-gradient(
circle at 80% -10%,
#182431 0,
#090b0e 40%
);
color:var(--text);
font-family:
Inter,
ui-sans-serif,
system-ui,
-apple-system,
BlinkMacSystemFont,
"Segoe UI",
sans-serif;
}

button,
input,
select,
textarea{
font:inherit;
}

button{
background:#11161c;
color:var(--text);
border:1px solid var(--border);
border-radius:9px;
padding:9px 13px;
cursor:pointer;
font-weight:700;
}

button:hover{
border-color:var(--border2);
transform:translateY(-1px);
}

button.primary{
background:#142638;
border-color:#315d80;
}

button.danger{
color:#ff9ba6;
border-color:#67323b;
}

button.success{
color:#8be5b8;
border-color:#2b604b;
}

input,
select,
textarea{
background:#0e1216;
border:1px solid var(--border);
border-radius:9px;
padding:10px 12px;
color:var(--text);
outline:none;
}

header{
position:sticky;
top:0;
z-index:50;
background:rgba(9,11,14,.9);
backdrop-filter:blur(18px);
border-bottom:1px solid var(--border);
}

.header{
height:76px;
max-width:1700px;
margin:auto;
padding:0 25px;
display:flex;
align-items:center;
justify-content:space-between;
}

.logo{
font-size:20px;
font-weight:900;
letter-spacing:-.5px;
}

.logo span{
color:var(--accent);
}

.status{
font-size:11px;
color:var(--green);
margin-top:4px;
}

.layout{
max-width:1700px;
margin:auto;
display:grid;
grid-template-columns:235px 1fr;
min-height:calc(100vh - 76px);
}

nav{
border-right:1px solid var(--border);
padding:20px 13px;
background:rgba(10,13,17,.5);
}

.nav-title{
font-size:10px;
color:#66717d;
text-transform:uppercase;
letter-spacing:.12em;
margin:18px 9px 7px;
}

.nav button{
width:100%;
text-align:left;
margin:2px 0;
background:transparent;
border-color:transparent;
font-weight:600;
color:#aeb8c3;
}

.nav button.active{
background:#151d25;
border-color:#273541;
color:#fff;
}

main{
padding:28px;
overflow:hidden;
}

.view{
display:none;
}

.view.active{
display:block;
}

.top{
display:flex;
justify-content:space-between;
align-items:flex-start;
gap:15px;
margin-bottom:24px;
}

h1{
font-size:27px;
margin:0;
letter-spacing:-.7px;
}

.subtitle{
color:var(--muted);
font-size:13px;
margin-top:5px;
}

.actions{
display:flex;
gap:8px;
flex-wrap:wrap;
}

.grid{
display:grid;
grid-template-columns:
repeat(auto-fit,minmax(190px,1fr));
gap:12px;
}

.card{
background:
linear-gradient(
145deg,
rgba(20,25,31,.96),
rgba(14,17,21,.96)
);
border:1px solid var(--border);
border-radius:14px;
padding:18px;
box-shadow:
0 14px 40px rgba(0,0,0,.12);
}

.label{
color:var(--muted);
font-size:10px;
text-transform:uppercase;
letter-spacing:.11em;
}

.value{
font-size:27px;
font-weight:850;
margin-top:7px;
}

.small{
font-size:12px;
color:var(--muted);
margin-top:5px;
}

.section{
margin-top:18px;
}

.section-title{
font-size:12px;
font-weight:800;
text-transform:uppercase;
letter-spacing:.1em;
margin-bottom:9px;
color:#b9c4ce;
}

.panel{
background:#101419;
border:1px solid var(--border);
border-radius:14px;
padding:18px;
}

.table-wrap{
overflow:auto;
border:1px solid var(--border);
border-radius:12px;
}

table{
width:100%;
border-collapse:collapse;
min-width:700px;
}

th,
td{
padding:11px 12px;
border-bottom:1px solid var(--border);
font-size:12px;
text-align:left;
white-space:nowrap;
}

th{
color:#73808d;
font-size:10px;
text-transform:uppercase;
letter-spacing:.08em;
background:#0d1115;
}

.badge{
display:inline-flex;
padding:4px 8px;
border-radius:99px;
font-size:10px;
border:1px solid var(--border);
}

.badge.ok{
color:var(--green);
border-color:#285e48;
}

.badge.warn{
color:var(--yellow);
border-color:#665425;
}

.badge.danger{
color:var(--red);
border-color:#633039;
}

.two{
display:grid;
grid-template-columns:1fr 1fr;
gap:14px;
}

.company-grid{
display:grid;
grid-template-columns:
repeat(auto-fill,minmax(245px,1fr));
gap:10px;
}

.company-card{
border:1px solid var(--border);
background:#0f1419;
border-radius:12px;
padding:15px;
cursor:pointer;
}

.company-card:hover{
border-color:#45586b;
background:#141a20;
}

.company-name{
font-weight:800;
}

.company-meta{
color:var(--muted);
font-size:11px;
margin-top:5px;
}

.search{
width:100%;
margin-bottom:12px;
}

.alert{
padding:12px;
border:1px solid var(--border);
border-radius:10px;
margin-bottom:8px;
font-size:12px;
}

.alert.critical{
border-color:#74303b;
background:#1c1013;
}

.alert.warning{
border-color:#675326;
background:#1a160c;
}

.alert.info{
border-color:#2d506d;
background:#0f1821;
}

.support-bar{
display:none;
position:sticky;
top:77px;
z-index:40;
background:#241c0c;
border:1px solid #624d24;
color:#f1d88a;
padding:11px 15px;
border-radius:0 0 10px 10px;
}

.modal{
position:fixed;
inset:0;
background:rgba(0,0,0,.7);
backdrop-filter:blur(8px);
display:none;
align-items:center;
justify-content:center;
z-index:100;
padding:20px;
}

.modal-box{
width:min(900px,100%);
max-height:90vh;
overflow:auto;
background:#11161b;
border:1px solid var(--border2);
border-radius:16px;
padding:22px;
}

.kv{
display:grid;
grid-template-columns:180px 1fr;
gap:7px;
font-size:12px;
}

.kv div{
padding:8px;
border-bottom:1px solid var(--border);
}

.empty{
padding:35px;
text-align:center;
color:var(--muted);
}

@media(max-width:1050px){

.layout{
grid-template-columns:1fr;
}

nav{
border-right:0;
border-bottom:1px solid var(--border);
display:flex;
overflow:auto;
gap:5px;
}

.nav-title{
display:none;
}

.nav button{
min-width:max-content;
}

main{
padding:18px;
}

.two{
grid-template-columns:1fr;
}

}

</style>

</head>

<body>

<header>

<div class="header">

<div>
<div class="logo">
MASTER <span>CONTROL CENTER</span>
</div>

<div class="status">
● PLATFORM CONTROL
</div>
</div>

<div class="actions">
<button onclick="refreshAll()">
Atualizar
</button>

<button onclick="location.href='/master'">
Master clássico
</button>
</div>

</div>

</header>

<div id="supportBar" class="support-bar"></div>

<div class="layout">

<nav class="nav">

<div class="nav-title">
Plataforma
</div>

<button
class="active"
onclick="view('dashboard',this)">
Dashboard
</button>

<button onclick="view('companies',this)">
Empresas
</button>

<button onclick="view('users',this)">
Usuários
</button>

<button onclick="view('support',this)">
Suporte
</button>

<div class="nav-title">
Negócio
</div>

<button onclick="view('billing',this)">
Billing
</button>

<button onclick="view('analytics',this)">
Analytics
</button>

<button onclick="view('flags',this)">
Feature Flags
</button>

<div class="nav-title">
Segurança
</div>

<button onclick="view('security',this)">
Segurança
</button>

<button onclick="view('sessions',this)">
Sessões
</button>

<button onclick="view('audit',this)">
Auditoria
</button>

<button onclick="view('alerts',this)">
Alertas
</button>

<div class="nav-title">
Infraestrutura
</div>

<button onclick="view('diagnostics',this)">
Diagnóstico
</button>

<button onclick="view('backups',this)">
Backups
</button>

<button onclick="view('system',this)">
Sistema
</button>

<button onclick="view('data',this)">
Data Center
</button>

<button onclick="view('maintenance',this)">
Manutenção
</button>

<button onclick="view('integrations',this)">
API / Integrações
</button>

</nav>

<main>

<!-- DASHBOARD -->

<section id="dashboard" class="view active">

<div class="top">

<div>
<h1>Command Center</h1>
<div class="subtitle">
Visão global da plataforma SaaS
</div>
</div>

</div>

<div id="dashboardCards" class="grid"></div>

<div class="section">

<div class="section-title">
Saúde da plataforma
</div>

<div id="healthCards" class="grid"></div>

</div>

</section>


<!-- COMPANIES -->

<section id="companies" class="view">

<div class="top">

<div>
<h1>Empresas</h1>
<div class="subtitle">
Controle completo da base de clientes
</div>
</div>

<div class="actions">
<button
class="primary"
onclick="createCompany()">
+ Nova empresa
</button>
</div>

</div>

<input
id="companySearch"
class="search"
placeholder="Pesquisar empresa..."
oninput="renderCompanies()"
>

<div
id="companiesList"
class="company-grid">
</div>

</section>


<!-- USERS -->

<section id="users" class="view">

<div class="top">

<div>
<h1>Usuários</h1>
<div class="subtitle">
Identidade e acesso global
</div>
</div>

</div>

<div class="table-wrap">
<table>
<thead>
<tr>
<th>ID</th>
<th>Nome</th>
<th>Usuário</th>
<th>Cargo</th>
<th>Empresa</th>
<th>Status</th>
<th>Ações</th>
</tr>
</thead>
<tbody id="usersTable"></tbody>
</table>
</div>

</section>


<!-- SUPPORT -->

<section id="support" class="view">

<div class="top">

<div>
<h1>Central de Suporte</h1>
<div class="subtitle">
Visualização operacional das empresas
</div>
</div>

</div>

<div id="supportCurrent" class="panel">
Nenhum atendimento ativo.
</div>

<div class="section">

<div class="section-title">
Snapshot da empresa
</div>

<div id="supportSnapshot" class="panel">
Selecione uma empresa em Empresas e inicie o modo suporte.
</div>

</div>

</section>


<!-- BILLING -->

<section id="billing" class="view">

<div class="top">

<div>
<h1>Billing</h1>
<div class="subtitle">
Planos, mensalidades e receita recorrente
</div>
</div>

</div>

<div id="billingCards" class="grid"></div>

<div class="section">

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Empresa</th>
<th>Plano</th>
<th>Mensalidade</th>
<th>Status</th>
<th>Vencimento</th>
<th>Trial</th>
</tr>
</thead>
<tbody id="billingTable"></tbody>
</table>
</div>

</div>

</section>


<!-- ANALYTICS -->

<section id="analytics" class="view">

<div class="top">

<div>
<h1>Analytics</h1>
<div class="subtitle">
Utilização e desempenho por empresa
</div>
</div>

</div>

<div id="analyticsCards" class="grid"></div>

<div class="section">

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Empresa</th>
<th>Usuários</th>
<th>Produtos</th>
<th>Clientes</th>
<th>Vendas</th>
<th>Receita</th>
</tr>
</thead>
<tbody id="analyticsTable"></tbody>
</table>
</div>

</div>

</section>


<!-- FLAGS -->

<section id="flags" class="view">

<div class="top">

<div>
<h1>Feature Flags</h1>
<div class="subtitle">
Ative ou desative módulos por empresa
</div>
</div>

</div>

<div id="flagsContent"></div>

</section>


<!-- SECURITY -->

<section id="security" class="view">

<div class="top">

<div>
<h1>Segurança</h1>
<div class="subtitle">
Eventos críticos e histórico de segurança
</div>
</div>

</div>

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Data</th>
<th>Evento</th>
<th>Severidade</th>
<th>Empresa</th>
<th>IP</th>
<th>Detalhes</th>
</tr>
</thead>
<tbody id="securityTable"></tbody>
</table>
</div>

</section>


<!-- SESSIONS -->

<section id="sessions" class="view">

<div class="top">

<div>
<h1>Sessões</h1>
<div class="subtitle">
Sessões ativas na plataforma
</div>
</div>

<button
class="danger"
onclick="globalLogout()">
Logout global
</button>

</div>

<div class="table-wrap">
<table>
<thead>
<tr>
<th>ID</th>
<th>Usuário</th>
<th>Empresa</th>
<th>Cargo</th>
<th>Token</th>
</tr>
</thead>
<tbody id="sessionsTable"></tbody>
</table>
</div>

</section>


<!-- AUDIT -->

<section id="audit" class="view">

<div class="top">

<div>
<h1>Auditoria</h1>
<div class="subtitle">
Todas as ações administrativas
</div>
</div>

</div>

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Data</th>
<th>Ação</th>
<th>Empresa</th>
<th>Usuário</th>
<th>Detalhes</th>
<th>IP</th>
</tr>
</thead>
<tbody id="auditTable"></tbody>
</table>
</div>

</section>


<!-- ALERTS -->

<section id="alerts" class="view">

<div class="top">

<div>
<h1>Alertas</h1>
<div class="subtitle">
Problemas que merecem atenção
</div>
</div>

</div>

<div id="alertsContent"></div>

</section>


<!-- DIAGNOSTICS -->

<section id="diagnostics" class="view">

<div class="top">

<div>
<h1>Diagnóstico</h1>
<div class="subtitle">
Integridade e consistência do sistema
</div>
</div>

<button onclick="loadDiagnostics()">
Executar diagnóstico
</button>

</div>

<div id="diagnosticsContent"></div>

</section>


<!-- BACKUPS -->

<section id="backups" class="view">

<div class="top">

<div>
<h1>Backups</h1>
<div class="subtitle">
Proteção e recuperação
</div>
</div>

<button
class="primary"
onclick="createBackup()">
Criar backup agora
</button>

</div>

<div id="backupsContent"></div>

</section>


<!-- SYSTEM -->

<section id="system" class="view">

<div class="top">

<div>
<h1>Sistema</h1>
<div class="subtitle">
Infraestrutura da aplicação
</div>
</div>

</div>

<div id="systemContent"></div>

</section>


<!-- DATA -->

<section id="data" class="view">

<div class="top">

<div>
<h1>Data Center</h1>
<div class="subtitle">
Estrutura real do SQLite
</div>
</div>

</div>

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Tipo</th>
<th>Nome</th>
<th>Registros</th>
</tr>
</thead>
<tbody id="dataTable"></tbody>
</table>
</div>

</section>


<!-- MAINTENANCE -->

<section id="maintenance" class="view">

<div class="top">

<div>
<h1>Manutenção</h1>
<div class="subtitle">
Controle global da disponibilidade
</div>
</div>

</div>

<div class="panel">

<div class="actions">

<button
class="danger"
onclick="setMaintenance(true)">
Ativar manutenção
</button>

<button
class="success"
onclick="setMaintenance(false)">
Desativar manutenção
</button>

</div>

<div class="section">

<textarea
id="maintenanceMessage"
style="width:100%;min-height:100px"
placeholder="Mensagem exibida durante a manutenção..."
></textarea>

</div>

<div class="section">

<label>
<input
type="checkbox"
id="maintenanceMaster"
checked
>
Permitir acesso do Master
</label>

</div>

</div>

</section>


<!-- INTEGRATIONS -->

<section id="integrations" class="view">

<div class="top">

<div>
<h1>API / Integrações</h1>
<div class="subtitle">
Chaves, webhooks e integrações futuras
</div>
</div>

</div>

<div class="two">

<div class="panel">

<div class="section-title">
API Keys
</div>

<div id="keysContent"></div>

</div>

<div class="panel">

<div class="section-title">
Webhooks
</div>

<div id="webhooksContent"></div>

</div>

</div>

</section>

</main>

</div>


<!-- MODAL -->

<div id="modal" class="modal">

<div class="modal-box">

<div id="modalContent"></div>

<div style="margin-top:15px">
<button onclick="closeModal()">
Fechar
</button>
</div>

</div>

</div>


<script>

let companies=[];
let users=[];

async function api(url, options={}){

const response=await fetch(
url,
{
credentials:"same-origin",
...options
}
);

let data;

try{
data=await response.json();
}catch(e){
throw new Error(
"Resposta inválida do servidor."
);
}

if(!response.ok || data.ok===false){
throw new Error(
data.error ||
"Erro na operação."
);
}

return data;
}

function money(value){

return Number(value||0).toLocaleString(
"pt-BR",
{
style:"currency",
currency:"BRL"
}
);
}

function escapeHtml(value){

return String(value ?? "")
.replaceAll("&","&amp;")
.replaceAll("<","&lt;")
.replaceAll(">","&gt;")
.replaceAll('"',"&quot;")
.replaceAll("'","&#039;");
}

function view(name,button){

document
.querySelectorAll(".view")
.forEach(x=>x.classList.remove("active"));

document
.getElementById(name)
.classList.add("active");

document
.querySelectorAll(".nav button")
.forEach(x=>x.classList.remove("active"));

if(button){
button.classList.add("active");
}

loadView(name);
}

async function loadView(name){

try{

if(name==="dashboard")
await loadDashboard();

if(name==="companies")
await loadCompanies();

if(name==="users")
await loadUsers();

if(name==="billing")
await loadBilling();

if(name==="analytics")
await loadAnalytics();

if(name==="flags")
await loadFlags();

if(name==="security")
await loadSecurity();

if(name==="sessions")
await loadSessions();

if(name==="audit")
await loadAudit();

if(name==="alerts")
await loadAlerts();

if(name==="diagnostics")
await loadDiagnostics();

if(name==="backups")
await loadBackups();

if(name==="system")
await loadSystem();

if(name==="data")
await loadData();

if(name==="maintenance")
await loadMaintenance();

if(name==="integrations")
await loadIntegrations();

if(name==="support")
await loadSupport();

}catch(error){

alert(error.message);

}

}

async function loadDashboard(){

const data=await api(
"/master/plus/api/dashboard"
);

const d=data.dashboard;

dashboardCards.innerHTML=`

<div class="card">
<div class="label">Empresas</div>
<div class="value">${d.companies}</div>
</div>

<div class="card">
<div class="label">Usuários</div>
<div class="value">${d.users}</div>
</div>

<div class="card">
<div class="label">Produtos</div>
<div class="value">${d.products}</div>
</div>

<div class="card">
<div class="label">Clientes</div>
<div class="value">${d.customers}</div>
</div>

<div class="card">
<div class="label">Vendas</div>
<div class="value">${d.sales}</div>
</div>

<div class="card">
<div class="label">Sessões</div>
<div class="value">${d.online_sessions}</div>
</div>

<div class="card">
<div class="label">Receita registrada</div>
<div class="value">${money(d.revenue)}</div>
</div>

<div class="card">
<div class="label">Resultado</div>
<div class="value">${money(d.result)}</div>
</div>

`;

healthCards.innerHTML=`

<div class="card">
<div class="label">Banco</div>
<div class="value">●</div>
<div class="small">${d.database_mb} MB</div>
</div>

<div class="card">
<div class="label">API</div>
<div class="value">●</div>
<div class="small">Operacional</div>
</div>

<div class="card">
<div class="label">Controle Master</div>
<div class="value">●</div>
<div class="small">Protegido</div>
</div>

`;

}

async function loadCompanies(){

const data=await api(
"/master/plus/api/companies"
);

companies=data.companies;

renderCompanies();

}

function renderCompanies(){

const q=(
document.getElementById(
"companySearch"
).value||""
).toLowerCase();

const list=companies.filter(c=>
JSON.stringify(c)
.toLowerCase()
.includes(q)
);

companiesList.innerHTML=
list.map(c=>`

<div
class="company-card"
onclick="companyDetails(${c.id})">

<div class="company-name">
${escapeHtml(c.name||"Empresa")}
</div>

<div class="company-meta">
ID ${c.id}
· ${c.users||0} usuários
· ${c.products||0} produtos
· ${c.sales||0} vendas
 · Segmento ${escapeHtml(c.segment||"LOJA")}
</div>

<div style="margin-top:10px">

<span class="badge ${
c.active===0
?"danger"
:"ok"
}">
${c.active===0?"Suspensa":"Ativa"}
</span>

</div>

</div>

`).join("");

if(!list.length){

companiesList.innerHTML=
'<div class="empty">Nenhuma empresa.</div>';

}

}

async function companyDetails(id){

const data=await api(
"/master/plus/api/company/"+id
);

const d=data.data;
const c=d.company;
const cfg=d.config||{};
const u=d.usage||{};

openModal(`

<h2>${escapeHtml(c.name||"Empresa")}</h2>

<div class="subtitle">
Empresa #${c.id}
</div>

<div class="section">

<div class="grid">

<div class="card">
<div class="label">Usuários</div>
<div class="value">${u.users||0}</div>
</div>

<div class="card">
<div class="label">Produtos</div>
<div class="value">${u.products||0}</div>
</div>

<div class="card">
<div class="label">Clientes</div>
<div class="value">${u.customers||0}</div>
</div>

<div class="card">
<div class="label">Vendas</div>
<div class="value">${u.sales||0}</div>
</div>

<div class="card">
<div class="label">Receita</div>
<div class="value">${money(u.revenue)}</div>
</div>

</div>

</div>

<div class="section">

<div class="section-title">
Plano
</div>

<div class="kv">

<div>Plano</div>
<div>${escapeHtml(cfg.plan||"standard")}</div>

<div>Mensalidade</div>
<div>${money(cfg.monthly_price)}</div>

<div>Status</div>
<div>${escapeHtml(cfg.billing_status||"active")}</div>

<div>Vencimento</div>
<div>${escapeHtml(cfg.billing_due_date||"-")}</div>

</div>

</div>

<div class="section">

<div class="actions">

<button
class="primary"
onclick="startSupport(${c.id})">
Entrar em modo suporte
</button>

<button
onclick="companyLogout(${c.id})">
Encerrar sessões
</button>

${
c.active===0
?
`<button
class="success"
onclick="activateCompany(${c.id})">
Reativar
</button>`
:
`<button
class="danger"
onclick="suspendCompany(${c.id})">
Suspender
</button>`
}

</div>

</div>

`);

}

async function createCompany(){

const name=prompt(
"Nome da nova empresa:"
);

if(!name)
return;

const segment=String(prompt("Segmento da empresa: LOJA ou RESTAURANTE","LOJA")||"").trim().toUpperCase();
if(!["LOJA","RESTAURANTE"].includes(segment)){
alert("Informe LOJA ou RESTAURANTE.");
return;
}

const data=await api(
"/master/plus/api/company/create",
{
method:"POST",
headers:{
"Content-Type":"application/json"
},
body:JSON.stringify({
name,
segment
})
}
);

alert(
"Empresa criada. ID: "+
data.company_id
);

await loadCompanies();

}

async function suspendCompany(id){

if(!confirm(
"Suspender esta empresa e encerrar suas sessões?"
))
return;

await api(
"/master/plus/api/company/"+id+"/suspend",
{
method:"POST"
}
);

closeModal();
await loadCompanies();

}

async function activateCompany(id){

await api(
"/master/plus/api/company/"+id+"/activate",
{
method:"POST"
}
);

closeModal();
await loadCompanies();

}

async function companyLogout(id){

if(!confirm(
"Encerrar todas as sessões desta empresa?"
))
return;

const data=await api(
"/master/plus/api/company/"+id+"/logout-all",
{
method:"POST"
}
);

alert(
data.closed_sessions+
" sessões encerradas."
);

}

async function loadUsers(){

const data=await api(
"/master/plus/api/users"
);

users=data.users;

usersTable.innerHTML=
users.map(u=>`

<tr>

<td>${u.id}</td>

<td>${escapeHtml(u.name||"")}</td>

<td>${escapeHtml(u.username||"")}</td>

<td>

<select
onchange="changeRole(${u.id},this.value)"
${String(u.role).toLowerCase()==="master"?"disabled":""}
>

<option
${u.role==="admin"?"selected":""}
value="admin">
admin
</option>

<option
${u.role==="gerente"?"selected":""}
value="gerente">
gerente
</option>

<option
${u.role==="vendedor"?"selected":""}
value="vendedor">
vendedor
</option>

<option
${u.role==="caixa"?"selected":""}
value="caixa">
caixa
</option>

</select>

</td>

<td>
${escapeHtml(u.company_name||"MASTER")}
</td>

<td>
<span class="badge ${
u.active?"ok":"danger"
}">
${u.active?"Ativo":"Bloqueado"}
</span>
</td>

<td>

${
String(u.role).toLowerCase()==="master"
?
"Protegido"
:
`
<button
onclick="toggleUser(${u.id})">
${u.active?"Bloquear":"Ativar"}
</button>

<button
onclick="logoutUser(${u.id})">
Logout
</button>
`
}

</td>

</tr>

`).join("");

}

async function toggleUser(id){

await api(
"/master/plus/api/user/"+id+"/toggle",
{
method:"POST"
}
);

await loadUsers();

}

async function changeRole(id,role){

await api(
"/master/plus/api/user/"+id+"/role",
{
method:"POST",
headers:{
"Content-Type":"application/json"
},
body:JSON.stringify({role})
}
);

await loadUsers();

}

async function logoutUser(id){

await api(
"/master/plus/api/user/"+id+"/logout",
{
method:"POST"
}
);

alert("Sessões encerradas.");

}

async function startSupport(id){

const reason=prompt(
"Motivo do acesso de suporte:",
"suporte"
);

if(reason===null)
return;

const data=await api(
"/master/plus/api/support/start/"+id,
{
method:"POST",
headers:{
"Content-Type":"application/json"
},
body:JSON.stringify({
reason
})
}
);

closeModal();

supportBar.style.display="block";

supportBar.innerHTML=
"⚠ MODO SUPORTE ATIVO · "+
escapeHtml(data.company_name)+
" · "+
escapeHtml(reason)+
' <button onclick="stopSupport()">Encerrar</button>';

await loadSupport();

}

async function stopSupport(){

await api(
"/master/plus/api/support/stop",
{
method:"POST"
}
);

supportBar.style.display="none";

await loadSupport();

}

async function loadSupport(){

const data=await api(
"/master/plus/api/support/current"
);

if(!data.active){

supportCurrent.innerHTML=
"Nenhum atendimento ativo.";

return;

}

supportCurrent.innerHTML=`

<div class="alert warning">

<strong>Modo suporte ativo</strong>

<br>

Empresa:
${escapeHtml(
data.support.company_name
)}

<br>

Motivo:
${escapeHtml(
data.support.reason
)}

<br><br>

<button
onclick="loadSupportSnapshot(
${data.support.company_id}
)">
Abrir visão operacional
</button>

<button
onclick="stopSupport()">
Encerrar suporte
</button>

</div>

`;

}

async function loadSupportSnapshot(id){

const data=await api(
"/master/plus/api/support/snapshot/"+id
);

const s=data.snapshot;
const u=s.usage||{};

supportSnapshot.innerHTML=`

<div class="grid">

<div class="card">
<div class="label">Usuários</div>
<div class="value">${u.users}</div>
</div>

<div class="card">
<div class="label">Produtos</div>
<div class="value">${u.products}</div>
</div>

<div class="card">
<div class="label">Clientes</div>
<div class="value">${u.customers}</div>
</div>

<div class="card">
<div class="label">Vendas</div>
<div class="value">${u.sales}</div>
</div>

<div class="card">
<div class="label">Receita</div>
<div class="value">${money(u.revenue)}</div>
</div>

</div>

<div class="section-title">
Vendas recentes
</div>

<pre style="white-space:pre-wrap;color:#aeb8c3">
${escapeHtml(
JSON.stringify(
s.recent_sales||[],
null,
2
)
)}
</pre>

`;

}

async function loadBilling(){

const data=await api(
"/master/plus/api/billing"
);

billingCards.innerHTML=`

<div class="card">
<div class="label">MRR</div>
<div class="value">${money(data.mrr)}</div>
</div>

<div class="card">
<div class="label">Empresas</div>
<div class="value">${data.companies.length}</div>
</div>

`;

billingTable.innerHTML=
data.companies.map(c=>`

<tr>

<td>${escapeHtml(c.company_name)}</td>

<td>${escapeHtml(c.plan||"-")}</td>

<td>${money(c.monthly_price)}</td>

<td>
<span class="badge ${
c.billing_status==="active"
?"ok"
:"warn"
}">
${escapeHtml(c.billing_status||"-")}
</span>
</td>

<td>${escapeHtml(c.billing_due_date||"-")}</td>

<td>${escapeHtml(c.trial_until||"-")}</td>

</tr>

`).join("");

}

async function loadAnalytics(){

const data=await api(
"/master/plus/api/analytics"
);

const a=data.analytics;

analyticsCards.innerHTML=`

<div class="card">
<div class="label">Empresas</div>
<div class="value">${a.total_companies}</div>
</div>

<div class="card">
<div class="label">Com atividade</div>
<div class="value">${a.active_companies}</div>
</div>

<div class="card">
<div class="label">Sem vendas</div>
<div class="value">${a.inactive_companies}</div>
</div>

`;

analyticsTable.innerHTML=
a.companies.map(c=>`

<tr>

<td>${escapeHtml(c.name)}</td>
<td>${c.users}</td>
<td>${c.products}</td>
<td>${c.customers}</td>
<td>${c.sales}</td>
<td>${money(c.revenue)}</td>

</tr>

`).join("");

}

async function loadFlags(){

if(!companies.length)
await loadCompanies();

flagsContent.innerHTML=
companies.map(c=>`

<div class="card" style="margin-bottom:10px">

<div class="company-name">
${escapeHtml(c.name)}
</div>

<div class="actions" style="margin-top:10px">

${[
["sales","Vendas"],
["stock","Estoque"],
["purchases","Compras"],
["customers","Clientes"],
["reports","Relatórios"],
["whatsapp","WhatsApp"],
["nfe","NF-e"],
["payments","Pagamentos"]
].map(f=>`

<button
onclick="toggleFlag(
${c.id},
'${f[0]}'
)">
${f[1]}
</button>

`).join("")}

</div>

</div>

`).join("");

}

async function toggleFlag(companyId,feature){

const enabled=confirm(
"Ativar "+feature+" para esta empresa?"
);

await api(
"/master/plus/api/flag/"+companyId,
{
method:"POST",
headers:{
"Content-Type":"application/json"
},
body:JSON.stringify({
feature,
enabled
})
}
);

alert(
feature+
": "+
(enabled?"ATIVADO":"DESATIVADO")
);

}

async function loadSecurity(){

const data=await api(
"/master/plus/api/security"
);

securityTable.innerHTML=
data.events.map(e=>`

<tr>

<td>${escapeHtml(e.created_at)}</td>
<td>${escapeHtml(e.event_type)}</td>
<td>${escapeHtml(e.severity)}</td>
<td>${e.company_id||"-"}</td>
<td>${escapeHtml(e.ip||"-")}</td>
<td>${escapeHtml(e.details||"")}</td>

</tr>

`).join("");

}

async function loadSessions(){

const data=await api(
"/master/plus/api/sessions"
);

sessionsTable.innerHTML=
data.sessions.map(s=>`

<tr>

<td>${s.token}</td>
<td>${escapeHtml(s.name||"")}</td>
<td>${escapeHtml(s.company_name||"MASTER")}</td>
<td>${escapeHtml(s.role||"")}</td>
<td>${escapeHtml(
String(s.token||"").slice(0,16)
)}...</td>

</tr>

`).join("");

}

async function globalLogout(){

if(!confirm(
"ENCERRAR TODAS as sessões dos clientes?"
))
return;

const data=await api(
"/master/plus/api/sessions/logout-all",
{
method:"POST"
}
);

alert(
data.closed_sessions+
" sessões encerradas."
);

await loadSessions();

}

async function loadAudit(){

const data=await api(
"/master/plus/api/audit"
);

auditTable.innerHTML=
data.audit.map(a=>`

<tr>

<td>${escapeHtml(a.created_at)}</td>
<td>${escapeHtml(a.action)}</td>
<td>${a.company_id||"-"}</td>
<td>${a.target_user_id||"-"}</td>
<td>${escapeHtml(a.details||"")}</td>
<td>${escapeHtml(a.ip||"-")}</td>

</tr>

`).join("");

}

async function loadAlerts(){

const data=await api(
"/master/plus/api/alerts"
);

if(!data.alerts.length){

alertsContent.innerHTML=
'<div class="empty">Nenhum alerta crítico encontrado.</div>';

return;

}

alertsContent.innerHTML=
data.alerts.map(a=>`

<div class="alert ${a.severity}">

<strong>
${escapeHtml(a.type)}
</strong>

<br>

${escapeHtml(a.message)}

${
a.company
?
"<br>Empresa: "+
escapeHtml(a.company)
:
""
}

</div>

`).join("");

}

async function loadDiagnostics(){

const data=await api(
"/master/plus/api/diagnostics"
);

const d=data.diagnostics;

diagnosticsContent.innerHTML=`

<div class="grid">

<div class="card">
<div class="label">Banco</div>
<div class="value">
${d.database?"OK":"ERRO"}
</div>
</div>

<div class="card">
<div class="label">Integrity Check</div>
<div class="value">
${escapeHtml(d.integrity)}
</div>
</div>

<div class="card">
<div class="label">Usuários órfãos</div>
<div class="value">
${d.orphan_users}
</div>
</div>

<div class="card">
<div class="label">Empresas sem admin</div>
<div class="value">
${d.companies_without_admin}
</div>
</div>

<div class="card">
<div class="label">Sessões inválidas</div>
<div class="value">
${d.invalid_sessions}
</div>
</div>

</div>

<div class="section panel">

<pre style="white-space:pre-wrap">
${escapeHtml(
JSON.stringify(
d.tables,
null,
2
)
)}
</pre>

</div>

`;

}

async function loadBackups(){

const data=await api(
"/master/plus/api/backups"
);

if(!data.backups.length){

backupsContent.innerHTML=
'<div class="empty">Nenhum backup encontrado.</div>';

return;

}

backupsContent.innerHTML=`

<div class="table-wrap">

<table>

<thead>

<tr>
<th>Arquivo</th>
<th>Tamanho</th>
<th>Modificado</th>
</tr>

</thead>

<tbody>

${data.backups.map(b=>`

<tr>

<td>${escapeHtml(b.name)}</td>
<td>${b.size_mb} MB</td>
<td>${escapeHtml(b.modified)}</td>

</tr>

`).join("")}

</tbody>

</table>

</div>

`;

}

async function createBackup(){

if(!confirm(
"Criar um backup do banco agora?"
))
return;

const data=await api(
"/master/plus/api/backup",
{
method:"POST"
}
);

alert(
"Backup criado:\n"+
data.file+
"\n"+
data.size_mb+
" MB"
);

await loadBackups();

}

async function loadSystem(){

const data=await api(
"/master/plus/api/system"
);

const s=data.system;

systemContent.innerHTML=`

<div class="grid">

${Object.entries(s).map(
([key,value])=>`

<div class="card">

<div class="label">
${escapeHtml(key)}
</div>

<div class="value"
style="font-size:18px">

${escapeHtml(value)}

</div>

</div>

`
).join("")}

</div>

`;

}

async function loadData(){

const data=await api(
"/master/plus/api/data"
);

dataTable.innerHTML=
data.objects.map(o=>`

<tr>

<td>${escapeHtml(o.type)}</td>

<td>${escapeHtml(o.name)}</td>

<td>${o.rows??"-"}</td>

</tr>

`).join("");

}

async function loadMaintenance(){

const data=await api(
"/master/plus/api/maintenance"
);

const m=data.maintenance||{};

maintenanceMessage.value=
m.message||
"";

maintenanceMaster.checked=
Boolean(m.allow_master);

}

async function setMaintenance(enabled){

const message=
maintenanceMessage.value||
"Sistema em manutenção.";

const allowMaster=
maintenanceMaster.checked;

await api(
"/master/plus/api/maintenance",
{
method:"POST",
headers:{
"Content-Type":"application/json"
},
body:JSON.stringify({
enabled,
message,
allow_master:allowMaster
})
}
);

alert(
enabled
?
"Modo manutenção ATIVADO."
:
"Modo manutenção DESATIVADO."
);

}

async function loadIntegrations(){

const keys=await api(
"/master/plus/api/integrations/keys"
);

const hooks=await api(
"/master/plus/api/integrations/webhooks"
);

keysContent.innerHTML=
keys.keys.length
?
keys.keys.map(k=>`

<div class="alert">

<strong>
${escapeHtml(k.name)}
</strong>

<br>

Empresa:
${k.company_id}

<br>

Prefixo:
${escapeHtml(k.key_prefix)}

<br>

Status:
${k.active?"Ativa":"Desativada"}

</div>

`).join("")
:
'<div class="empty">Nenhuma API Key.</div>';

webhooksContent.innerHTML=
hooks.webhooks.length
?
hooks.webhooks.map(w=>`

<div class="alert">

<strong>
${escapeHtml(w.name)}
</strong>

<br>

Empresa:
${w.company_id}

<br>

Evento:
${escapeHtml(w.event)}

<br>

URL:
${escapeHtml(w.url)}

</div>

`).join("")
:
'<div class="empty">Nenhum webhook.</div>';

}

function openModal(html){

modal.style.display="flex";
modalContent.innerHTML=html;

}

function closeModal(){

modal.style.display="none";

}

async function refreshAll(){

await loadDashboard();
await loadCompanies();

alert(
"Master atualizado."
);

}

async function startup(){

try{

await loadDashboard();
await loadCompanies();
await loadSupport();

}catch(error){

alert(
"Falha ao carregar Master: "+
error.message
);

}

}

startup();

</script>

</body>

</html>
"""


if __name__ == "__main__":

    ensure_tables()

    print(
        "MASTER CONTROL CENTER: OK"
    )


