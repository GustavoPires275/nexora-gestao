
import os
import json
import time
import shutil
import sqlite3
import secrets
import hashlib
import platform
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

try:
    import fastapi
except Exception:
    fastapi = None

STARTED_AT = datetime.now()
REGISTERED = False
_CONN = None
_GET_USER = None
_AUDIT = None
_HASH_PASSWORD = None
_NOW = None
_APP = None

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "loja.db"
BACKUP_DIR = BASE_DIR / "BACKUPS"
MAINTENANCE_FILE = BASE_DIR / "master_maintenance.json"

BACKUP_DIR.mkdir(exist_ok=True)


# ============================================================
# CORE
# ============================================================

def register_master(app, conn, get_user, audit, hash_password, now):
    global REGISTERED, _CONN, _GET_USER, _AUDIT, _HASH_PASSWORD, _NOW, _APP

    _CONN = conn
    _GET_USER = get_user
    _AUDIT = audit
    _HASH_PASSWORD = hash_password
    _NOW = now
    _APP = app

    if REGISTERED:
        return

    REGISTERED = True

    # Migração aditiva (fusão MASTER): colunas que master_plus / master_enterprise / Company Control V1 já usam,
    # mas que bancos antigos não têm (causavam erro 500 em salvar limites e em suspender/ativar). Idempotente.
    try:
        _mc = conn()
        for _tbl, _col, _ddl in (
            ("master_company_config", "max_customers", "INTEGER DEFAULT 5000"),
            ("master_company_config", "max_sales_month", "INTEGER DEFAULT 10000"),
            ("master_security_events", "details", "TEXT DEFAULT ''"),
        ):
            _cols = {r[1] for r in _mc.execute(f"PRAGMA table_info({_tbl})").fetchall()}
            if _cols and _col not in _cols:
                _mc.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_ddl}")
        if _mc.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'").fetchone():
            _company_cols = {r[1] for r in _mc.execute("PRAGMA table_info(companies)").fetchall()}
            if "segment" not in _company_cols:
                _mc.execute("ALTER TABLE companies ADD COLUMN segment TEXT NOT NULL DEFAULT 'LOJA' CHECK(segment IN ('LOJA','RESTAURANTE'))")
                # Legacy rows become restaurants only when existing restaurant operation data proves it.
                for _restaurant_table in ("restaurant_tables", "tabs", "restaurant_orders"):
                    _exists = _mc.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (_restaurant_table,),
                    ).fetchone()
                    if _exists and "company_id" in {r[1] for r in _mc.execute(f"PRAGMA table_info({_restaurant_table})").fetchall()}:
                        _mc.execute(
                            f"UPDATE companies SET segment='RESTAURANTE' WHERE id IN "
                            f"(SELECT DISTINCT company_id FROM {_restaurant_table} WHERE company_id IS NOT NULL)"
                        )
        _mc.commit()
        _mc.close()
    except Exception as _e:
        print("[MASTER CONTROL] migração de colunas ignorada:", repr(_e))

    @app.middleware("http")
    async def master_maintenance_guard(request: Request, call_next):
        path = request.url.path

        allowed = (
            path.startswith("/master")
            or path in {
                "/api/login",
                "/api/logout",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/favicon.ico",
            }
            or path.startswith("/static")
            or path.startswith("/assets")
            or path.startswith("/ws")
        )

        if allowed:
            return await call_next(request)

        try:
            if MAINTENANCE_FILE.exists():
                data = json.loads(MAINTENANCE_FILE.read_text(encoding="utf-8"))
                if data.get("enabled"):
                    return JSONResponse(
                        status_code=503,
                        content={
                            "ok": False,
                            "maintenance": True,
                            "message": data.get(
                                "message",
                                "Sistema temporariamente indisponível para manutenção."
                            ),
                        },
                    )
        except Exception:
            pass

        return await call_next(request)

    @app.get("/master", response_class=HTMLResponse)
    @app.get("/master/", response_class=HTMLResponse)
    async def master_page(request: Request):
        user = _current_user(request)
        if not _is_master(user):
            return HTMLResponse("Acesso negado", status_code=403)
        return HTMLResponse(_HTML)

    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------

    @app.get("/master/api/dashboard")
    async def master_dashboard(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        started = time.perf_counter()

        companies = _table_exists("companies")
        users = _table_exists("users")
        products = _table_exists("products")
        customers = _table_exists("customers")
        sales = _table_exists("sales")
        expenses = _table_exists("expenses")

        today = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")

        result = {
            "ok": True,
            "companies": _count("companies"),
            "active_companies": _count_where("companies", "active = 1"),
            "inactive_companies": _count_where("companies", "active = 0"),
            "users": _count("users"),
            "active_users": _count_where("users", "active = 1"),
            "products": _count("products"),
            "customers": _count("customers"),
            "sales": _count("sales"),
            "expenses": _sum_amount("expenses"),
            "revenue": _sum_amount("sales"),
            "today_sales": _sum_date("sales", today),
            "month_sales": _sum_month("sales", month),
            "today_count": _count_date("sales", today),
            "month_count": _count_month("sales", month),
            "active_sessions": _count("sessions"),
            "database_size": _database_size(),
            "storage": _storage(),
            "backup": _latest_backup(),
            "recent_activity": _recent_audit(8),
        }

        result["estimated_result"] = (
            result["revenue"] - result["expenses"]
        )

        result["elapsed_ms"] = round(
            (time.perf_counter() - started) * 1000, 2
        )

        return result

    @app.get("/master/api/evolution")
    async def master_evolution(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        months = []

        base = datetime.now().replace(day=1)

        for offset in range(11, -1, -1):
            year = base.year
            month = base.month - offset

            while month <= 0:
                month += 12
                year -= 1

            key = f"{year:04d}-{month:02d}"

            months.append(
                {
                    "month": key,
                    "revenue": _sum_month("sales", key),
                    "expenses": _sum_month("expenses", key),
                    "sales": _count_month("sales", key),
                }
            )

        return {"ok": True, "months": months}

    # --------------------------------------------------------
    # Companies
    # --------------------------------------------------------

    @app.get("/master/api/companies")
    async def master_companies(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("companies"):
            return {"ok": True, "items": []}

        rows = []

        for row in _select_all("companies"):
            cid = row.get("id")

            rows.append(
                {
                    "id": cid,
                    "name": _value(row, "name", "company_name", default=f"Empresa {cid}"),
                    "active": bool(_value(row, "active", default=1)),
                    "segment": _value(row, "segment", default="LOJA"),
                    "created_at": _value(row, "created_at", "created", "createdAt"),
                    "users": _count_company("users", cid),
                    "products": _count_company("products", cid),
                    "customers": _count_company("customers", cid),
                    "sales": _count_company("sales", cid),
                    "expenses": _sum_company("expenses", cid),
                    "revenue": _sum_company("sales", cid),
                    "last_activity": _company_last_activity(cid),
                }
            )

        return {"ok": True, "items": rows}

    @app.get("/master/api/companies/{company_id}")
    async def master_company_detail(company_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        company = _get_by_id("companies", company_id)

        if not company:
            return JSONResponse(
                {"ok": False, "error": "Empresa não encontrada"},
                status_code=404,
            )

        # ========================================================
        # SUPER MASTER — DADOS SaaS DA EMPRESA
        # ========================================================

        saas = {
            "plan": "standard",
            "monthly_price": 0,
            "billing_status": "active",
            "billing_due_date": None,
            "trial_until": None,
            "max_users": 0,
            "max_products": 0,
            "max_customers": 0,
            "max_sales_month": 0,
            "max_storage_mb": 0,
            "support_enabled": 0,
            "maintenance_blocked": 0,
            "notes": "",
        }

        try:
            if _table_exists("master_company_config"):
                config_cols = _columns("master_company_config")

                if "company_id" in config_cols:
                    config = conn.execute(
                        """
                        SELECT *
                        FROM master_company_config
                        WHERE company_id=?
                        LIMIT 1
                        """,
                        (company_id,),
                    ).fetchone()

                    if config:
                        config_dict = dict(config)

                        for key in saas:
                            if key in config_dict:
                                saas[key] = config_dict[key]

            # Garante que o limite de clientes/vendas também seja
            # exposto mesmo quando a versão antiga do schema não tiver.
            if _table_exists("master_company_limits"):
                limit_cols = _columns("master_company_limits")

                if "company_id" in limit_cols:
                    limits = conn.execute(
                        """
                        SELECT *
                        FROM master_company_limits
                        WHERE company_id=?
                        LIMIT 1
                        """,
                        (company_id,),
                    ).fetchone()

                    if limits:
                        limits_dict = dict(limits)

                        for key in (
                            "max_users",
                            "max_products",
                            "max_customers",
                            "max_sales_month",
                            "max_storage_mb",
                        ):
                            if key in limits_dict:
                                value = limits_dict[key]

                                # Só substitui quando houver valor válido.
                                if value is not None:
                                    saas[key] = value

        except Exception:
            # O detalhe da empresa não pode quebrar caso alguma
            # tabela SaaS esteja temporariamente indisponível.
            pass

        usage = {
            "users": _count_company("users", company_id),
            "products": _count_company("products", company_id),
            "customers": _count_company("customers", company_id),
            "sales": _count_company("sales", company_id),
            "expenses": _count_company("expenses", company_id),
            "purchases": _count_company("purchases", company_id),
            "stock_movements": _count_company("stock_movements", company_id),
            "revenue": _sum_company("sales", company_id),
        }

        return {
            "ok": True,
            "company": company,
            "users": _company_rows("users", company_id),
            "products": _company_rows("products", company_id, 30),
            "customers": _company_rows("customers", company_id, 30),
            "sales": _company_rows("sales", company_id, 30),
            "expenses": _company_rows("expenses", company_id, 30),

            # Núcleo SaaS do SUPER MASTER.
            "saas": saas,

            # Uso atual da empresa.
            "usage": usage,

            # Compatibilidade com a interface atual.
            "stats": {
                "users": usage["users"],
                "products": usage["products"],
                "customers": usage["customers"],
                "sales": usage["sales"],
                "revenue": usage["revenue"],
                "expenses": usage["expenses"],
            },
        }

    @app.post("/master/api/companies")
    async def master_create_company(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        data = await request.json()
        name = str(data.get("name", "")).strip()
        segment = str(data.get("segment", "LOJA") or "").strip().upper()

        if segment not in {"LOJA", "RESTAURANTE"}:
            return JSONResponse(
                {"ok": False, "error": "Segmento deve ser LOJA ou RESTAURANTE"},
                status_code=400,
            )

        if not name:
            return JSONResponse(
                {"ok": False, "error": "Nome da empresa é obrigatório"},
                status_code=400,
            )

        if not _table_exists("companies"):
            return JSONResponse(
                {"ok": False, "error": "Tabela companies não existe"},
                status_code=500,
            )

        cols = _columns("companies")

        values = {}

        if "name" in cols:
            values["name"] = name
        elif "company_name" in cols:
            values["company_name"] = name
        else:
            return JSONResponse(
                {"ok": False, "error": "Schema companies incompatível"},
                status_code=500,
            )

        if "active" in cols:
            values["active"] = 1

        if "segment" in cols:
            values["segment"] = segment

        if "created_at" in cols:
            values["created_at"] = _now()

        try:
            cid = _insert("companies", values)
            _audit_safe(
                "CREATE",
                "company",
                cid,
                f"Empresa criada: {name}; segmento={segment}",
                request,
            )

            return {
                "ok": True,
                "id": cid,
                "message": "Empresa criada com sucesso",
            }

        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

    @app.put("/master/api/companies/{company_id}")
    async def master_edit_company(company_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        data = await request.json()
        name = str(data.get("name", "")).strip()
        segment = data.get("segment")
        if segment is not None:
            segment = str(segment).strip().upper()
            if segment not in {"LOJA", "RESTAURANTE"}:
                return JSONResponse(
                    {"ok": False, "error": "Segmento deve ser LOJA ou RESTAURANTE"},
                    status_code=400,
                )

        if not name:
            return JSONResponse(
                {"ok": False, "error": "Nome obrigatório"},
                status_code=400,
            )

        cols = _columns("companies")
        field = "name" if "name" in cols else "company_name"

        if field not in cols:
            return JSONResponse(
                {"ok": False, "error": "Campo de nome não encontrado"},
                status_code=500,
            )

        with _CONN() as db:
            existing = db.execute(
                "SELECT id,segment FROM companies WHERE id=?", (company_id,)
            ).fetchone()
            if not existing:
                return JSONResponse({"ok": False, "error": "Empresa não encontrada"}, status_code=404)
            if segment == "LOJA" and str(existing["segment"] or "LOJA").upper() == "RESTAURANTE":
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "tabs" in tables:
                    open_tabs = db.execute(
                        "SELECT COUNT(*) FROM tabs WHERE company_id=? AND status IN ('open','paying')",
                        (company_id,),
                    ).fetchone()[0]
                    if open_tabs:
                        return JSONResponse(
                            {"ok": False, "error": "Feche as comandas abertas antes de alterar o segmento."},
                            status_code=409,
                        )
            assignments = [f"{_qi(field)} = ?"]
            params = [name]
            if segment is not None and "segment" in cols:
                assignments.append('"segment" = ?')
                params.append(segment)
            params.append(company_id)
            db.execute(
                f"UPDATE companies SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            if segment is not None and segment != str(existing["segment"] or "LOJA").upper():
                db.execute(
                    "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id=?)",
                    (company_id,),
                )
            db.commit()

        _audit_safe(
            "UPDATE",
            "company",
            company_id,
            f"Empresa atualizada: {name}" + (f"; segmento={segment}" if segment is not None else ""),
            request,
        )

        return {"ok": True}

    @app.post("/master/api/companies/{company_id}/status")
    async def master_company_status(company_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        data = await request.json()
        active = 1 if bool(data.get("active")) else 0

        cols = _columns("companies")

        if "active" not in cols:
            return JSONResponse(
                {"ok": False, "error": "Campo active não existe"},
                status_code=500,
            )

        with _CONN() as db:
            db.execute(
                "UPDATE companies SET active = ? WHERE id = ?",
                (active, company_id),
            )
            db.commit()

        _audit_safe(
            "COMPANY_REACTIVATED" if active else "COMPANY_SUSPENDED",
            "company",
            company_id,
            "Status alterado pelo Master",
            request,
        )

        return {"ok": True, "active": bool(active)}

    # --------------------------------------------------------
    # Users
    # --------------------------------------------------------

    @app.get("/master/api/users")
    async def master_users(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("users"):
            return {"ok": True, "items": []}

        rows = []

        with _CONN() as db:
            data = db.execute(
                "SELECT * FROM users ORDER BY id DESC"
            ).fetchall()

            for r in data:
                item = dict(r)

                item.pop("password_hash", None)
                item.pop("password_salt", None)

                company_id = item.get("company_id")

                item["company_name"] = _company_name(company_id)
                item["sessions"] = _user_sessions(item.get("id"))

                rows.append(item)

        return {"ok": True, "items": rows}

    @app.post("/master/api/users/{user_id}/role")
    async def master_change_role(user_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        target = _get_by_id("users", user_id)

        if not target:
            return JSONResponse(
                {"ok": False, "error": "Usuário não encontrado"},
                status_code=404,
            )

        if str(target.get("role", "")).lower() == "master":
            return JSONResponse(
                {"ok": False, "error": "A conta Master é protegida"},
                status_code=403,
            )

        data = await request.json()
        role = str(data.get("role", "")).lower().strip()

        allowed = {"admin", "gerente", "vendedor", "caixa"}

        if role not in allowed:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Função inválida",
                    "allowed": sorted(allowed),
                },
                status_code=400,
            )

        with _CONN() as db:
            db.execute(
                "UPDATE users SET role = ? WHERE id = ?",
                (role, user_id),
            )
            db.commit()

        _audit_safe(
            "ROLE_CHANGE",
            "user",
            user_id,
            f"Função alterada para {role}",
            request,
        )

        return {"ok": True, "role": role}

    @app.post("/master/api/users/{user_id}/toggle")
    async def master_toggle_user(user_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        target = _get_by_id("users", user_id)

        if not target:
            return JSONResponse(
                {"ok": False, "error": "Usuário não encontrado"},
                status_code=404,
            )

        if str(target.get("role", "")).lower() == "master":
            return JSONResponse(
                {"ok": False, "error": "A conta Master é protegida"},
                status_code=403,
            )

        current = bool(target.get("active", 1))
        new_status = 0 if current else 1

        with _CONN() as db:
            db.execute(
                "UPDATE users SET active = ? WHERE id = ?",
                (new_status, user_id),
            )
            db.commit()

        _audit_safe(
            "USER_ACTIVATED" if new_status else "USER_DEACTIVATED",
            "user",
            user_id,
            "Status alterado pelo Master",
            request,
        )

        return {"ok": True, "active": bool(new_status)}

    @app.post("/master/api/users/{user_id}/reset-password")
    async def master_reset_password(user_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        target = _get_by_id("users", user_id)

        if not target:
            return JSONResponse(
                {"ok": False, "error": "Usuário não encontrado"},
                status_code=404,
            )

        if str(target.get("role", "")).lower() == "master":
            return JSONResponse(
                {"ok": False, "error": "Não é permitido resetar a conta Master por aqui"},
                status_code=403,
            )

        data = await request.json()
        password = str(data.get("password", ""))

        if len(password) < 6:
            return JSONResponse(
                {"ok": False, "error": "Senha deve possuir pelo menos 6 caracteres"},
                status_code=400,
            )

        salt = secrets.token_hex(16)
        _hp = _HASH_PASSWORD(password, salt)
        password_hash = _hp[1] if isinstance(_hp, (tuple, list)) else _hp

        cols = _columns("users")

        if "password_salt" not in cols or "password_hash" not in cols:
            return JSONResponse(
                {"ok": False, "error": "Campos de senha incompatíveis"},
                status_code=500,
            )

        with _CONN() as db:
            db.execute(
                """
                UPDATE users
                SET password_salt = ?, password_hash = ?
                WHERE id = ?
                """,
                (salt, password_hash, user_id),
            )

            if _table_exists("sessions"):
                db.execute(
                    "DELETE FROM sessions WHERE user_id = ?",
                    (user_id,),
                )

            db.commit()

        _audit_safe(
            "PASSWORD_RESET",
            "user",
            user_id,
            "Senha redefinida pelo Master",
            request,
        )

        return {"ok": True}

    @app.post("/master/api/users/{user_id}/force-logout")
    async def master_force_logout(user_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        deleted = 0

        if _table_exists("sessions"):
            with _CONN() as db:
                cur = db.execute(
                    "DELETE FROM sessions WHERE user_id = ?",
                    (user_id,),
                )
                deleted = cur.rowcount
                db.commit()

        _audit_safe(
            "SESSION_REVOKED",
            "user",
            user_id,
            f"{deleted} sessão(ões) encerrada(s)",
            request,
        )

        return {"ok": True, "deleted": deleted}

    @app.post("/master/api/users/create-admin")
    async def master_create_admin(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        data = await request.json()

        company_id = int(data.get("company_id", 0))
        name = str(data.get("name", "")).strip()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        if not company_id or not name or not username or len(password) < 6:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Empresa, nome, usuário e senha de 6+ caracteres são obrigatórios",
                },
                status_code=400,
            )

        if not _get_by_id("companies", company_id):
            return JSONResponse(
                {"ok": False, "error": "Empresa não encontrada"},
                status_code=404,
            )

        cols = _columns("users")

        if "username" in cols:
            with _CONN() as db:
                exists = db.execute(
                    "SELECT id FROM users WHERE username = ?",
                    (username,),
                ).fetchone()

                if exists:
                    return JSONResponse(
                        {"ok": False, "error": "Username já existe"},
                        status_code=409,
                    )

        salt = secrets.token_hex(16)
        _hp = _HASH_PASSWORD(password, salt)
        password_hash = _hp[1] if isinstance(_hp, (tuple, list)) else _hp

        values = {
            "name": name,
            "username": username,
            "password_salt": salt,
            "password_hash": password_hash,
            "role": "admin",
            "active": 1,
            "company_id": company_id,
        }

        if "created_at" in cols:
            values["created_at"] = _now()

        try:
            uid = _insert(
                "users",
                {k: v for k, v in values.items() if k in cols},
            )

            _audit_safe(
                "CREATE",
                "user",
                uid,
                f"Administrador criado para empresa {company_id}",
                request,
            )

            return {"ok": True, "id": uid}

        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

    @app.post("/master/api/companies/{company_id}/sessions/revoke")
    async def master_company_sessions(company_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("sessions"):
            return {"ok": True, "deleted": 0}

        user_ids = []

        if _table_exists("users"):
            with _CONN() as db:
                rows = db.execute(
                    "SELECT id FROM users WHERE company_id = ?",
                    (company_id,),
                ).fetchall()
                user_ids = [r["id"] for r in rows]

        deleted = 0

        with _CONN() as db:
            for uid in user_ids:
                cur = db.execute(
                    "DELETE FROM sessions WHERE user_id = ?",
                    (uid,),
                )
                deleted += cur.rowcount

            db.commit()

        _audit_safe(
            "SESSION_REVOKED",
            "company",
            company_id,
            f"{deleted} sessão(ões) encerrada(s)",
            request,
        )

        return {"ok": True, "deleted": deleted}

    # --------------------------------------------------------
    # Sessions
    # --------------------------------------------------------

    @app.get("/master/api/sessions")
    async def master_sessions(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("sessions"):
            return {"ok": True, "items": []}

        rows = []

        with _CONN() as db:
            session_rows = db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC"
            ).fetchall()

            for r in session_rows:
                item = dict(r)

                token = item.get("token")
                item.pop("token", None)

                uid = item.get("user_id")
                user = _get_by_id("users", uid) if uid else None

                item["user_id"] = uid
                item["username"] = user.get("username") if user else None
                item["role"] = user.get("role") if user else None
                item["company_id"] = user.get("company_id") if user else None
                item["company_name"] = (
                    _company_name(user.get("company_id"))
                    if user
                    else None
                )

                item["session_ref"] = (
                    hashlib.sha256(str(token).encode()).hexdigest()[:12]
                    if token
                    else None
                )

                rows.append(item)

        return {
            "ok": True,
            "items": rows,
            "note": "Tokens reais nunca são exibidos.",
        }

    @app.post("/master/api/sessions/global-revoke")
    async def master_global_logout(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        current_token = request.cookies.get("session")
        deleted = 0

        if _table_exists("sessions"):
            with _CONN() as db:
                if current_token:
                    cur = db.execute(
                        "DELETE FROM sessions WHERE token != ?",
                        (current_token,),
                    )
                else:
                    cur = db.execute("DELETE FROM sessions")

                deleted = cur.rowcount
                db.commit()

        _audit_safe(
            "SESSION_REVOKED",
            "system",
            None,
            f"Logout global executado. {deleted} sessões encerradas.",
            request,
        )

        return {"ok": True, "deleted": deleted}

    # --------------------------------------------------------
    # Audit
    # --------------------------------------------------------

    @app.get("/master/api/audit")
    async def master_audit(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("audit_log"):
            return {"ok": True, "items": []}

        limit = min(
            max(int(request.query_params.get("limit", "200")), 1),
            1000,
        )

        with _CONN() as db:
            rows = db.execute(
                f"SELECT * FROM audit_log ORDER BY rowid DESC LIMIT {limit}"
            ).fetchall()

        return {"ok": True, "items": [dict(r) for r in rows]}

    # --------------------------------------------------------
    # Finance
    # --------------------------------------------------------

    @app.get("/master/api/finance")
    async def master_finance(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        start = request.query_params.get("start")
        end = request.query_params.get("end")

        revenue = _sum_range("sales", start, end)
        expenses = _sum_range("expenses", start, end)

        return {
            "ok": True,
            "start": start,
            "end": end,
            "revenue": revenue,
            "expenses": expenses,
            "result": revenue - expenses,
            "sales_count": _count_range("sales", start, end),
            "ticket_average": (
                revenue / _count_range("sales", start, end)
                if _count_range("sales", start, end)
                else 0
            ),
            "companies": _finance_by_company(start, end),
        }

    # --------------------------------------------------------
    # Sales
    # --------------------------------------------------------

    @app.get("/master/api/sales")
    async def master_sales(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        limit = min(
            max(int(request.query_params.get("limit", "200")), 1),
            1000,
        )

        if not _table_exists("sales"):
            return {"ok": True, "items": []}

        rows = _company_rows("sales", None, limit)

        for item in rows:
            cid = item.get("company_id")
            item["company_name"] = _company_name(cid)

        return {
            "ok": True,
            "items": rows,
            "total": _count("sales"),
            "revenue": _sum_amount("sales"),
        }

    # --------------------------------------------------------
    # Products / Stock
    # --------------------------------------------------------

    @app.get("/master/api/products")
    async def master_products(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        limit = min(
            max(int(request.query_params.get("limit", "300")), 1),
            1000,
        )

        rows = _company_rows("products", None, limit)

        for item in rows:
            item["company_name"] = _company_name(item.get("company_id"))

        return {
            "ok": True,
            "items": rows,
            "total": _count("products"),
            "stock": _stock_total(),
            "low_stock": _low_stock(),
            "zero_stock": _zero_stock(),
        }

    @app.get("/master/api/stock")
    async def master_stock(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        return {
            "ok": True,
            "total_products": _count("products"),
            "total_stock": _stock_total(),
            "low_stock": _low_stock(),
            "zero_stock": _zero_stock(),
            "movements": _stock_movements(200),
        }

    # --------------------------------------------------------
    # Customers
    # --------------------------------------------------------

    @app.get("/master/api/customers")
    async def master_customers(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        rows = _company_rows("customers", None, 500)

        for item in rows:
            item["company_name"] = _company_name(item.get("company_id"))

        return {
            "ok": True,
            "items": rows,
            "total": _count("customers"),
        }

    # --------------------------------------------------------
    # Global search
    # --------------------------------------------------------

    @app.get("/master/api/search")
    async def master_search(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        q = str(request.query_params.get("q", "")).strip()

        if len(q) < 2:
            return {"ok": True, "items": []}

        result = []

        targets = [
            ("companies", ["name", "company_name"], "empresa"),
            ("users", ["name", "username"], "usuário"),
            ("products", ["name", "sku", "barcode", "code"], "produto"),
            ("customers", ["name", "cpf", "phone"], "cliente"),
        ]

        for table, possible_fields, kind in targets:
            if not _table_exists(table):
                continue

            cols = _columns(table)
            fields = [f for f in possible_fields if f in cols]

            if not fields:
                continue

            where = " OR ".join(
                f"CAST({_qi(f)} AS TEXT) LIKE ?" for f in fields
            )

            params = [f"%{q}%"] * len(fields)

            with _CONN() as db:
                rows = db.execute(
                    f"""
                    SELECT * FROM {_qi(table)}
                    WHERE {where}
                    ORDER BY id DESC
                    LIMIT 20
                    """,
                    params,
                ).fetchall()

            for row in rows:
                item = dict(row)

                item.pop("password_hash", None)
                item.pop("password_salt", None)

                result.append(
                    {
                        "type": kind,
                        "id": item.get("id"),
                        "name": (
                            item.get("name")
                            or item.get("username")
                            or item.get("company_name")
                            or item.get("sku")
                        ),
                        "company_id": item.get("company_id"),
                        "company_name": _company_name(
                            item.get("company_id")
                        ),
                    }
                )

        return {"ok": True, "items": result}

    # --------------------------------------------------------
    # Database
    # --------------------------------------------------------

    @app.get("/master/api/database")
    async def master_database(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        tables = _tables()

        counts = {}

        for table in tables:
            try:
                counts[table] = _count(table)
            except Exception:
                counts[table] = None

        return {
            "ok": True,
            "engine": "SQLite",
            "database": str(DB_FILE),
            "exists": DB_FILE.exists(),
            "size": _database_size(),
            "tables": tables,
            "table_count": len(tables),
            "counts": counts,
            "integrity": _integrity(),
        }

    @app.post("/master/api/database/integrity")
    async def master_database_integrity(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        return _integrity()

    # --------------------------------------------------------
    # Backups
    # --------------------------------------------------------

    @app.get("/master/api/backups")
    async def master_backups(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        return {
            "ok": True,
            "items": _backup_history(),
        }

    @app.post("/master/api/backup")
    async def master_backup(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        BACKUP_DIR.mkdir(exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = BACKUP_DIR / f"loja_{stamp}.db"

        try:
            source = sqlite3.connect(str(DB_FILE))
            target = sqlite3.connect(str(destination))

            with target:
                source.backup(target)

            target.close()
            source.close()

            size = destination.stat().st_size

            _audit_safe(
                "BACKUP_CREATED",
                "database",
                None,
                f"Backup criado: {destination.name}",
                request,
            )

            return {
                "ok": True,
                "file": destination.name,
                "size": size,
                "path": str(destination),
            }

        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )

    # --------------------------------------------------------
    # Alerts
    # --------------------------------------------------------

    @app.get("/master/api/alerts")
    async def master_alerts(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        alerts = []

        if _table_exists("companies"):
            inactive = _count_where("companies", "active = 0")
            if inactive:
                alerts.append(
                    {
                        "level": "warning",
                        "title": "Empresas inativas",
                        "message": f"{inactive} empresa(s) estão inativas.",
                    }
                )

        if _table_exists("users"):
            disabled = _count_where("users", "active = 0")
            if disabled:
                alerts.append(
                    {
                        "level": "info",
                        "title": "Usuários desativados",
                        "message": f"{disabled} usuário(s) estão desativados.",
                    }
                )

        if _table_exists("sessions"):
            sessions = _count("sessions")
            if sessions > 50:
                alerts.append(
                    {
                        "level": "warning",
                        "title": "Muitas sessões",
                        "message": f"{sessions} sessões ativas.",
                    }
                )

        zero = _zero_stock()
        if zero:
            alerts.append(
                {
                    "level": "warning",
                    "title": "Estoque zerado",
                    "message": f"{zero} produto(s) com estoque zerado.",
                }
            )

        low = _low_stock()
        if low:
            alerts.append(
                {
                    "level": "info",
                    "title": "Estoque baixo",
                    "message": f"{low} produto(s) com estoque baixo.",
                }
            )

        integrity = _integrity()

        if not integrity.get("ok"):
            alerts.append(
                {
                    "level": "critical",
                    "title": "Integridade do banco",
                    "message": integrity.get("error", "Falha na integridade."),
                }
            )

        backup = _latest_backup()

        if not backup:
            alerts.append(
                {
                    "level": "warning",
                    "title": "Sem backup",
                    "message": "Nenhum backup encontrado.",
                }
            )
        else:
            try:
                age = (
                    datetime.now()
                    - datetime.fromtimestamp(backup["mtime"])
                ).total_seconds()

                if age > 86400 * 3:
                    alerts.append(
                        {
                            "level": "warning",
                            "title": "Backup antigo",
                            "message": "O backup mais recente tem mais de 3 dias.",
                        }
                    )
            except Exception:
                pass

        if _table_exists("companies") and _table_exists("users"):
            for company in _select_all("companies"):
                cid = company.get("id")
                admins = _count_company_role("users", cid, "admin")

                if admins == 0:
                    alerts.append(
                        {
                            "level": "warning",
                            "title": "Empresa sem administrador",
                            "message": (
                                f"A empresa "
                                f"{_value(company, 'name', 'company_name', default=cid)} "
                                "não possui administrador."
                            ),
                            "company_id": cid,
                        }
                    )

        return {
            "ok": True,
            "items": alerts,
            "count": len(alerts),
        }

    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------

    @app.get("/master/api/health")
    async def master_health(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        started = time.perf_counter()

        db_ok = False
        db_error = None

        try:
            with _CONN() as db:
                db.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception as exc:
            db_error = str(exc)

        websocket = False

        try:
            for route in _APP.routes:
                if "websocket" in route.__class__.__name__.lower():
                    websocket = True
                    break
                if str(getattr(route, "path", "")).startswith("/ws"):
                    websocket = True
                    break
        except Exception:
            pass

        integrity = _integrity()

        return {
            "ok": db_ok and integrity.get("ok", False),
            "api": "online",
            "sqlite": "online" if db_ok else "offline",
            "sqlite_error": db_error,
            "integrity": integrity,
            "authentication": "integrated",
            "sessions": _count("sessions"),
            "websocket": "detected" if websocket else "not_detected",
            "fastapi": getattr(fastapi, "__version__", "unknown"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "process_started": STARTED_AT.isoformat(sep=" "),
            "response_ms": round(
                (time.perf_counter() - started) * 1000, 2
            ),
        }

    # --------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------

    @app.get("/master/api/diagnostics")
    async def master_diagnostics(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        checks = []

        required = [
            "companies",
            "users",
            "sessions",
            "audit_log",
            "products",
            "customers",
            "sales",
            "expenses",
        ]

        for table in required:
            exists = _table_exists(table)

            checks.append(
                {
                    "name": f"Tabela {table}",
                    "status": "OK" if exists else "WARNING",
                    "message": (
                        "Encontrada"
                        if exists
                        else "Tabela não encontrada"
                    ),
                }
            )

        tenant_tables = [
            "users",
            "products",
            "customers",
            "sales",
            "expenses",
        ]

        for table in tenant_tables:
            if not _table_exists(table):
                continue

            has_company = "company_id" in _columns(table)

            checks.append(
                {
                    "name": f"Isolamento {table}",
                    "status": "OK" if has_company else "WARNING",
                    "message": (
                        "company_id presente"
                        if has_company
                        else "company_id não encontrado"
                    ),
                }
            )

        integrity = _integrity()

        checks.append(
            {
                "name": "Integridade SQLite",
                "status": "OK" if integrity.get("ok") else "CRITICAL",
                "message": integrity.get("message", ""),
            }
        )

        return {
            "ok": all(
                c["status"] != "CRITICAL"
                for c in checks
            ),
            "checks": checks,
        }

    # --------------------------------------------------------
    # Maintenance
    # --------------------------------------------------------

    @app.get("/master/api/maintenance")
    async def master_get_maintenance(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        return _maintenance_state()

    @app.post("/master/api/maintenance")
    async def master_set_maintenance(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        data = await request.json()

        enabled = bool(data.get("enabled"))
        message = str(
            data.get(
                "message",
                "Sistema temporariamente indisponível para manutenção.",
            )
        ).strip()

        if not message:
            message = "Sistema temporariamente indisponível para manutenção."

        payload = {
            "enabled": enabled,
            "message": message,
            "updated_at": _now(),
        }

        MAINTENANCE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _audit_safe(
            "MAINTENANCE_ENABLED" if enabled else "MAINTENANCE_DISABLED",
            "system",
            None,
            message,
            request,
        )

        return {"ok": True, **payload}

    # --------------------------------------------------------
    # Platform
    # --------------------------------------------------------

    @app.get("/master/api/platform")
    async def master_platform(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        usage = _storage()

        return {
            "ok": True,
            "system": "Gestão Empresarial",
            "module": "Master Enterprise",
            "version": "Enterprise 1.0",
            "environment": os.getenv("ENVIRONMENT", "production"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database": str(DB_FILE),
            "database_size": _database_size(),
            "backup_directory": str(BACKUP_DIR),
            "host": os.getenv("HOST", "0.0.0.0"),
            "port": os.getenv("PORT", "8000"),
            "maintenance": _maintenance_state(),
            "storage": usage,
        }


# ============================================================
# AUTH
# ============================================================


    # ============================================================
    # SUPER MASTER V5.1 BACKEND
    # ============================================================
    @app.get("/master/api/v5/overview")
    async def master_v5_overview(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        companies = _select_all("companies") if _table_exists("companies") else []
        config = {}
        if _table_exists("master_company_config"):
            try:
                with _CONN() as db:
                    rows = db.execute("SELECT * FROM master_company_config").fetchall()
                config = {int(r["company_id"]): dict(r) for r in rows}
            except Exception:
                config = {}

        def num(v):
            try:
                return float(v or 0)
            except Exception:
                return 0.0

        plans = {}
        paying = 0
        trial = 0
        overdue = 0
        blocked = 0
        mrr = 0.0
        company_cards = []

        for c in companies:
            cid = c.get("id")
            cfg = config.get(int(cid), {})
            plan = str(cfg.get("plan") or "Sem plano")
            status = str(
                cfg.get("billing_status")
                or ("active" if c.get("active") else "inactive")
            ).lower()
            price = num(cfg.get("monthly_price"))
            plans[plan] = plans.get(plan, 0) + 1

            if status in {"active", "paid", "paying"}:
                paying += 1
                mrr += price
            elif status in {"trial", "trialing"}:
                trial += 1
            elif status in {"overdue", "past_due", "delinquent"}:
                overdue += 1

            if cfg.get("maintenance_blocked"):
                blocked += 1

            company_cards.append({
                "id": cid,
                "name": c.get("name") or c.get("company_name") or f"Empresa {cid}",
                "active": bool(c.get("active", 1)),
                "plan": plan,
                "billing_status": status,
                "monthly_price": price,
                "users": _count_company("users", cid),
                "products": _count_company("products", cid),
                "customers": _count_company("customers", cid),
                "sales": _count_company("sales", cid),
                "revenue": _sum_company("sales", cid),
                "maintenance_blocked": bool(cfg.get("maintenance_blocked")),
            })

        return {
            "ok": True,
            "generated_at": _now(),
            "kpis": {
                "companies": len(companies),
                "active_companies": sum(1 for c in companies if c.get("active", 1)),
                "users": _count("users"),
                "products": _count("products"),
                "customers": _count("customers"),
                "sales": _count("sales"),
                "revenue": _sum_amount("sales"),
                "expenses": _sum_amount("expenses"),
                "mrr": mrr,
                "paying": paying,
                "trial": trial,
                "overdue": overdue,
                "maintenance_blocked": blocked,
                "active_sessions": _count("sessions"),
            },
            "plans": plans,
            "companies": company_cards,
            "health": {
                "database_size": _database_size(),
                "storage": _storage(),
                "tables": len(_tables()),
                "integrity": _integrity(),
                "backup": _latest_backup(),
            },
            "recent_activity": _recent_audit(10),
        }

    @app.get("/master/api/v5/company/{company_id}")
    async def master_v5_company(company_id: int, request: Request):
        guard = _guard(request)
        if guard:
            return guard

        if not _table_exists("companies"):
            return JSONResponse(
                {"ok": False, "detail": "Tabela companies não existe."},
                status_code=500,
            )

        company = _get_by_id("companies", company_id)
        if not company:
            return JSONResponse(
                {"ok": False, "detail": "Empresa não encontrada."},
                status_code=404,
            )

        cfg = {}
        if _table_exists("master_company_config"):
            try:
                with _CONN() as db:
                    row = db.execute(
                        "SELECT * FROM master_company_config WHERE company_id = ? LIMIT 1",
                        (company_id,),
                    ).fetchone()
                if row:
                    cfg = dict(row)
            except Exception:
                pass

        usage = {
            "users": _count_company("users", company_id),
            "products": _count_company("products", company_id),
            "customers": _count_company("customers", company_id),
            "sales": _count_company("sales", company_id),
            "expenses": _count_company("expenses", company_id),
            "purchases": _count_company("purchases", company_id),
            "stock_movements": _count_company("stock_movements", company_id),
            "revenue": _sum_company("sales", company_id),
        }

        audit = []
        if _table_exists("audit_log"):
            try:
                cols = _columns("audit_log")
                if "company_id" in cols:
                    with _CONN() as db:
                        rows = db.execute(
                            "SELECT * FROM audit_log WHERE company_id = ? ORDER BY rowid DESC LIMIT 12",
                            (company_id,),
                        ).fetchall()
                    audit = [dict(r) for r in rows]
            except Exception:
                pass

        return {
            "ok": True,
            "company": dict(company),
            "config": cfg,
            "usage": usage,
            "audit": audit,
        }

    @app.get("/master/api/v5/health")
    async def master_v5_health(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        integrity = _integrity()
        return {
            "ok": True,
            "status": "healthy" if integrity in (True, "ok", "OK", None) else "attention",
            "database": {
                "size": _database_size(),
                "tables": len(_tables()),
                "integrity": integrity,
            },
            "storage": _storage(),
            "backup": _latest_backup(),
            "maintenance": _maintenance_state(),
            "sessions": _count("sessions"),
            "server_time": _now(),
        }

    @app.get("/master/api/v5/search")
    async def master_v5_search(request: Request, q: str = ""):
        guard = _guard(request)
        if guard:
            return guard

        term = str(q or "").strip()
        if len(term) < 2:
            return {"ok": True, "results": []}

        like = f"%{term}%"
        results = []

        specs = [
            ("companies", ["name", "company_name"], "empresa"),
            ("users", ["name", "username"], "usuário"),
            ("products", ["name", "sku", "barcode"], "produto"),
            ("customers", ["name", "phone", "email"], "cliente"),
        ]

        for table, fields, kind in specs:
            if not _table_exists(table):
                continue

            cols = _columns(table)
            valid = [f for f in fields if f in cols]
            if not valid:
                continue

            where = " OR ".join(f"{_qi(f)} LIKE ?" for f in valid)

            try:
                with _CONN() as db:
                    rows = db.execute(
                        f"SELECT * FROM {_qi(table)} WHERE {where} LIMIT 8",
                        tuple(like for _ in valid),
                    ).fetchall()

                for row in rows:
                    item = dict(row)
                    results.append({
                        "type": kind,
                        "id": item.get("id"),
                        "title": (
                            item.get("name")
                            or item.get("username")
                            or item.get("company_name")
                            or f"{kind.title()} {item.get('id')}"
                        ),
                        "subtitle": (
                            item.get("username")
                            or item.get("barcode")
                            or item.get("email")
                            or ""
                        ),
                        "company_id": item.get("company_id"),
                    })
            except Exception:
                continue

        return {"ok": True, "results": results[:30]}

    @app.get("/master/api/v5/alerts")
    async def master_v5_alerts(request: Request):
        guard = _guard(request)
        if guard:
            return guard

        alerts = []

        if _table_exists("companies"):
            inactive = _count_where("companies", "active = 0")
            if inactive:
                alerts.append({
                    "level": "warning",
                    "title": "Empresas inativas",
                    "message": f"{inactive} empresa(s) estão inativas.",
                })

        if _table_exists("master_company_config"):
            try:
                with _CONN() as db:
                    rows = db.execute(
                        """
                        SELECT company_id, plan, billing_status, billing_due_date,
                               maintenance_blocked
                        FROM master_company_config
                        """
                    ).fetchall()

                for r in rows:
                    status = str(r["billing_status"] or "").lower()

                    if status in {"overdue", "past_due", "delinquent"}:
                        alerts.append({
                            "level": "danger",
                            "title": "Cobrança pendente",
                            "message": (
                                f"Empresa {r['company_id']} está com "
                                f"status {r['billing_status']}."
                            ),
                            "company_id": r["company_id"],
                        })

                    if r["maintenance_blocked"]:
                        alerts.append({
                            "level": "warning",
                            "title": "Acesso em manutenção",
                            "message": (
                                f"Empresa {r['company_id']} está marcada "
                                "para bloqueio de manutenção."
                            ),
                            "company_id": r["company_id"],
                        })
            except Exception:
                pass

        integrity = _integrity()

        if integrity not in (True, "ok", "OK", None):
            alerts.append({
                "level": "danger",
                "title": "Banco de dados",
                "message": "A verificação de integridade exige atenção.",
            })

        return {"ok": True, "alerts": alerts[:50]}

    # ============================================================
    # FIM SUPER MASTER V5.1 BACKEND
    # ============================================================

def _current_user(request):
    """
    Autenticacao oficial do SUPER MASTER.

    O painel MASTER deve usar a mesma sessao/cookie
    validada pelo app.py. Isso evita divergencia entre
    autenticacoes paralelas.
    """
    try:
        import app
        return app.get_user(request)
    except Exception:
        return None


def _is_master(user):
    if not user:
        return False

    role = (
        user.get("role")
        if isinstance(user, dict)
        else getattr(user, "role", None)
    )

    return str(role).lower() == "master"


def _guard(request):
    user = _current_user(request)

    if not _is_master(user):
        return JSONResponse(
            {
                "ok": False,
                "error": "Acesso restrito ao Master",
            },
            status_code=403,
        )

    return None


# ============================================================
# DATABASE HELPERS
# ============================================================

def _qi(name):
    return '"' + str(name).replace('"', '""') + '"'


def _columns(table):
    if not _table_exists(table):
        return []

    with _CONN() as db:
        rows = db.execute(
            f"PRAGMA table_info({_qi(table)})"
        ).fetchall()

    return [r["name"] for r in rows]


def _tables():
    with _CONN() as db:
        rows = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
            """
        ).fetchall()

    return [r["name"] for r in rows]


def _table_exists(table):
    try:
        with _CONN() as db:
            row = db.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                LIMIT 1
                """,
                (table,),
            ).fetchone()

        return bool(row)

    except Exception:
        return False


def _count(table):
    if not _table_exists(table):
        return 0

    with _CONN() as db:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM {_qi(table)}"
        ).fetchone()

    return int(row["n"] or 0)


def _count_where(table, condition):
    if not _table_exists(table):
        return 0

    with _CONN() as db:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM {_qi(table)} WHERE {condition}"
        ).fetchone()

    return int(row["n"] or 0)


def _sum_amount(table):
    if not _table_exists(table):
        return 0

    col = _amount_column(table)

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"SELECT COALESCE(SUM(CAST({_qi(col)} AS REAL)),0) AS n FROM {_qi(table)}"
        ).fetchone()

    return float(row["n"] or 0)


def _amount_column(table):
    cols = _columns(table)

    candidates = [
        "total",
        "amount",
        "valor",
        "value",
        "total_value",
        "sale_total",
        "price_total",
        "net_total",
    ]

    for col in candidates:
        if col in cols:
            return col

    return None


def _date_column(table):
    cols = _columns(table)

    candidates = [
        "created_at",
        "date",
        "data",
        "sale_date",
        "sold_at",
        "created",
        "timestamp",
    ]

    for col in candidates:
        if col in cols:
            return col

    return None


def _sum_date(table, date_value):
    col = _date_column(table)

    if not col:
        return 0

    amount = _amount_column(table)

    if not amount:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(SUM(CAST({_qi(amount)} AS REAL)),0) AS n
            FROM {_qi(table)}
            WHERE date({_qi(col)}) = date(?)
            """,
            (date_value,),
        ).fetchone()

    return float(row["n"] or 0)


def _sum_month(table, month):
    col = _date_column(table)
    amount = _amount_column(table)

    if not col or not amount:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(SUM(CAST({_qi(amount)} AS REAL)),0) AS n
            FROM {_qi(table)}
            WHERE strftime('%Y-%m', {_qi(col)}) = ?
            """,
            (month,),
        ).fetchone()

    return float(row["n"] or 0)


def _count_date(table, date_value):
    col = _date_column(table)

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE date({_qi(col)}) = date(?)
            """,
            (date_value,),
        ).fetchone()

    return int(row["n"] or 0)


def _count_month(table, month):
    col = _date_column(table)

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE strftime('%Y-%m', {_qi(col)}) = ?
            """,
            (month,),
        ).fetchone()

    return int(row["n"] or 0)


def _sum_range(table, start, end):
    if not start and not end:
        return _sum_amount(table)

    col = _date_column(table)
    amount = _amount_column(table)

    if not col or not amount:
        return 0

    conditions = []
    params = []

    if start:
        conditions.append(f"date({_qi(col)}) >= date(?)")
        params.append(start)

    if end:
        conditions.append(f"date({_qi(col)}) <= date(?)")
        params.append(end)

    where = " AND ".join(conditions)

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(SUM(CAST({_qi(amount)} AS REAL)),0) AS n
            FROM {_qi(table)}
            WHERE {where}
            """,
            params,
        ).fetchone()

    return float(row["n"] or 0)


def _count_range(table, start, end):
    if not start and not end:
        return _count(table)

    col = _date_column(table)

    if not col:
        return 0

    conditions = []
    params = []

    if start:
        conditions.append(f"date({_qi(col)}) >= date(?)")
        params.append(start)

    if end:
        conditions.append(f"date({_qi(col)}) <= date(?)")
        params.append(end)

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE {' AND '.join(conditions)}
            """,
            params,
        ).fetchone()

    return int(row["n"] or 0)


def _count_company(table, company_id):
    if not _table_exists(table):
        return 0

    if "company_id" not in _columns(table):
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE company_id = ?
            """,
            (company_id,),
        ).fetchone()

    return int(row["n"] or 0)


def _sum_company(table, company_id):
    if not _table_exists(table):
        return 0

    if "company_id" not in _columns(table):
        return 0

    amount = _amount_column(table)

    if not amount:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(SUM(CAST({_qi(amount)} AS REAL)),0) AS n
            FROM {_qi(table)}
            WHERE company_id = ?
            """,
            (company_id,),
        ).fetchone()

    return float(row["n"] or 0)


def _count_company_role(table, company_id, role):
    if not _table_exists(table):
        return 0

    cols = _columns(table)

    if "company_id" not in cols or "role" not in cols:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE company_id = ? AND role = ?
            """,
            (company_id, role),
        ).fetchone()

    return int(row["n"] or 0)


def _get_by_id(table, value):
    if not _table_exists(table):
        return None

    if "id" not in _columns(table):
        return None

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT *
            FROM {_qi(table)}
            WHERE id = ?
            LIMIT 1
            """,
            (value,),
        ).fetchone()

    return dict(row) if row else None


def _select_all(table):
    if not _table_exists(table):
        return []

    with _CONN() as db:
        rows = db.execute(
            f"SELECT * FROM {_qi(table)} ORDER BY id DESC"
        ).fetchall()

    return [dict(r) for r in rows]


def _company_rows(table, company_id=None, limit=200):
    if not _table_exists(table):
        return []

    cols = _columns(table)

    with _CONN() as db:
        if company_id is not None and "company_id" in cols:
            rows = db.execute(
                f"""
                SELECT *
                FROM {_qi(table)}
                WHERE company_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (company_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                f"""
                SELECT *
                FROM {_qi(table)}
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item.pop("password_hash", None)
        item.pop("password_salt", None)
        result.append(item)

    return result


def _insert(table, values):
    fields = list(values.keys())

    placeholders = ", ".join(["?"] * len(fields))

    sql = f"""
        INSERT INTO {_qi(table)}
        ({", ".join(_qi(x) for x in fields)})
        VALUES ({placeholders})
    """

    with _CONN() as db:
        cur = db.execute(
            sql,
            [values[x] for x in fields],
        )
        db.commit()

        return cur.lastrowid


def _value(row, *keys, default=None):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]

    return default


# ============================================================
# SYSTEM
# ============================================================

def _database_size():
    try:
        return DB_FILE.stat().st_size
    except Exception:
        return 0


def _storage():
    try:
        total, used, free = shutil.disk_usage(str(BASE_DIR))

        return {
            "total": total,
            "used": used,
            "free": free,
            "used_percent": round((used / total) * 100, 2),
        }

    except Exception:
        return {
            "total": 0,
            "used": 0,
            "free": 0,
            "used_percent": 0,
        }


def _integrity():
    try:
        with _CONN() as db:
            row = db.execute("PRAGMA integrity_check").fetchone()

        value = row[0] if row else None

        return {
            "ok": value == "ok",
            "message": value or "Sem resposta",
        }

    except Exception as exc:
        return {
            "ok": False,
            "message": "Falha",
            "error": str(exc),
        }


def _backup_history():
    items = []

    if not BACKUP_DIR.exists():
        return items

    for file in sorted(
        BACKUP_DIR.glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            stat = file.stat()

            items.append(
                {
                    "name": file.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "date": datetime.fromtimestamp(
                        stat.st_mtime
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

        except Exception:
            continue

    return items[:100]


def _latest_backup():
    history = _backup_history()

    return history[0] if history else None


def _maintenance_state():
    if not MAINTENANCE_FILE.exists():
        return {
            "enabled": False,
            "message": "Sistema temporariamente indisponível para manutenção.",
        }

    try:
        return json.loads(
            MAINTENANCE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {
            "enabled": False,
            "message": "Sistema temporariamente indisponível para manutenção.",
        }


# ============================================================
# STOCK
# ============================================================

def _stock_column():
    if not _table_exists("products"):
        return None

    cols = _columns("products")

    for col in [
        "stock",
        "quantity",
        "qty",
        "estoque",
        "current_stock",
        "stock_quantity",
    ]:
        if col in cols:
            return col

    return None


def _stock_total():
    col = _stock_column()

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(
                SUM(CAST({_qi(col)} AS REAL)), 0
            ) AS n
            FROM products
            """
        ).fetchone()

    return float(row["n"] or 0)


def _low_stock():
    col = _stock_column()

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM products
            WHERE CAST({_qi(col)} AS REAL) > 0
              AND CAST({_qi(col)} AS REAL) <= 5
            """
        ).fetchone()

    return int(row["n"] or 0)


def _zero_stock():
    col = _stock_column()

    if not col:
        return 0

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM products
            WHERE CAST({_qi(col)} AS REAL) <= 0
            """
        ).fetchone()

    return int(row["n"] or 0)


def _stock_movements(limit=200):
    if not _table_exists("stock_movements"):
        return []

    rows = _company_rows(
        "stock_movements",
        None,
        limit,
    )

    for item in rows:
        item["company_name"] = _company_name(
            item.get("company_id")
        )

    return rows


# ============================================================
# USERS / COMPANIES
# ============================================================

def _company_name(company_id):
    if not company_id:
        return None

    row = _get_by_id("companies", company_id)

    if not row:
        return None

    return _value(
        row,
        "name",
        "company_name",
        default=f"Empresa {company_id}",
    )


def _user_sessions(user_id):
    if not _table_exists("sessions"):
        return 0

    with _CONN() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return int(row["n"] or 0)


def _company_last_activity(company_id):
    if not _table_exists("audit_log"):
        return None

    cols = _columns("audit_log")

    company_field = (
        "company_id"
        if "company_id" in cols
        else None
    )

    date_field = _date_column("audit_log")

    if not company_field or not date_field:
        return None

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT {_qi(date_field)} AS d
            FROM audit_log
            WHERE company_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (company_id,),
        ).fetchone()

    return row["d"] if row else None


def _recent_audit(limit=8):
    if not _table_exists("audit_log"):
        return []

    with _CONN() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM audit_log
            ORDER BY rowid DESC
            LIMIT {int(limit)}
            """
        ).fetchall()

    return [dict(r) for r in rows]


def _finance_by_company(start, end):
    result = []

    if not _table_exists("companies"):
        return result

    for company in _select_all("companies"):
        cid = company.get("id")

        revenue = _sum_company_range(
            "sales",
            cid,
            start,
            end,
        )

        expenses = _sum_company_range(
            "expenses",
            cid,
            start,
            end,
        )

        sales_count = _count_company_range(
            "sales",
            cid,
            start,
            end,
        )

        result.append(
            {
                "company_id": cid,
                "company_name": _value(
                    company,
                    "name",
                    "company_name",
                    default=f"Empresa {cid}",
                ),
                "revenue": revenue,
                "expenses": expenses,
                "result": revenue - expenses,
                "sales": sales_count,
                "ticket": (
                    revenue / sales_count
                    if sales_count
                    else 0
                ),
            }
        )

    return result


def _sum_company_range(table, company_id, start, end):
    if not _table_exists(table):
        return 0

    cols = _columns(table)

    if "company_id" not in cols:
        return 0

    amount = _amount_column(table)
    date = _date_column(table)

    if not amount:
        return 0

    conditions = ["company_id = ?"]
    params = [company_id]

    if start and date:
        conditions.append(f"date({_qi(date)}) >= date(?)")
        params.append(start)

    if end and date:
        conditions.append(f"date({_qi(date)}) <= date(?)")
        params.append(end)

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COALESCE(
                SUM(CAST({_qi(amount)} AS REAL)), 0
            ) AS n
            FROM {_qi(table)}
            WHERE {" AND ".join(conditions)}
            """,
            params,
        ).fetchone()

    return float(row["n"] or 0)


def _count_company_range(table, company_id, start, end):
    if not _table_exists(table):
        return 0

    cols = _columns(table)

    if "company_id" not in cols:
        return 0

    date = _date_column(table)

    conditions = ["company_id = ?"]
    params = [company_id]

    if start and date:
        conditions.append(f"date({_qi(date)}) >= date(?)")
        params.append(start)

    if end and date:
        conditions.append(f"date({_qi(date)}) <= date(?)")
        params.append(end)

    with _CONN() as db:
        row = db.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM {_qi(table)}
            WHERE {" AND ".join(conditions)}
            """,
            params,
        ).fetchone()

    return int(row["n"] or 0)


# ============================================================
# AUDIT
# ============================================================

def _audit_safe(action, entity, entity_id, details, request):
    try:
        user = _current_user(request)

        attempts = [
            lambda: _AUDIT(
                user,
                action,
                entity,
                entity_id,
                details,
            ),
            lambda: _AUDIT(
                user=user,
                action=action,
                entity=entity,
                entity_id=entity_id,
                details=details,
            ),
            lambda: _AUDIT(
                action,
                entity,
                entity_id,
                details,
            ),
        ]

        for attempt in attempts:
            try:
                attempt()
                return
            except TypeError:
                continue

    except Exception:
        pass


def _now():
    try:
        return _NOW()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# UI
# ============================================================

_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXORA MASTER | Enterprise Management Platform</title>
<link rel="icon" type="image/svg+xml" href="/static/nexora-mark.svg">
<style>
*{box-sizing:border-box}
:root{
 --bg:#07090d;
 --panel:#0d1118;
 --panel2:#111722;
 --line:#202938;
 --text:#eef2f7;
 --muted:#8e9aaa;
 --gold:#d6b45b;
 --gold2:#f0d889;
 --green:#45d483;
 --red:#ff5f6d;
 --orange:#ffb454;
 --blue:#62a7ff;
}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
body{overflow-x:hidden}
button,input{font:inherit}
button{cursor:pointer}
.app{display:flex;min-height:100vh}
.side{
 width:260px;
 background:#090c11;
 border-right:1px solid var(--line);
 position:fixed;
 left:0;top:0;bottom:0;
 overflow-y:auto;
 z-index:20
}
.brand{padding:25px 20px 20px;border-bottom:1px solid var(--line)}
.brand b{display:block;font-size:19px;letter-spacing:1px;color:var(--gold2)}
.brand span{display:block;margin-top:7px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.4px}
.nav{padding:12px}
.nav button{
 width:100%;
 display:flex;
 align-items:center;
 gap:10px;
 border:0;
 background:transparent;
 color:#aab3c1;
 padding:12px 13px;
 border-radius:9px;
 margin-bottom:4px;
 text-align:left;
 transition:.15s
}
.nav button:hover{background:#121822;color:white}
.nav button.active{background:#181710;color:var(--gold2);box-shadow:inset 3px 0 0 var(--gold)}
.nav .section-label{
 color:#566171;
 font-size:10px;
 text-transform:uppercase;
 letter-spacing:1.5px;
 padding:15px 12px 7px
}
.main{margin-left:260px;width:calc(100% - 260px);min-width:0}
.top{
 position:sticky;
 top:0;
 z-index:10;
 background:rgba(7,9,13,.94);
 backdrop-filter:blur(10px);
 border-bottom:1px solid var(--line);
 padding:17px 25px;
 display:flex;
 justify-content:space-between;
 align-items:center;
 gap:20px
}
.title h1{margin:0;font-size:23px}
.title p{margin:5px 0 0;color:var(--muted);font-size:13px}
.pill{
 border:1px solid #554822;
 color:var(--gold2);
 background:#17140b;
 padding:8px 12px;
 border-radius:999px;
 font-size:11px;
 white-space:nowrap
}
#content{padding:25px}
.grid{
 display:grid;
 grid-template-columns:repeat(4,minmax(0,1fr));
 gap:14px
}
.card{
 background:linear-gradient(145deg,var(--panel),#0b0f15);
 border:1px solid var(--line);
 border-radius:12px;
 padding:17px;
 min-width:0
}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.card .v{font-size:25px;font-weight:700;margin-top:9px;word-break:break-word}
.card .s{font-size:11px;color:#697586;margin-top:7px}
.section{margin-top:16px}
.two{display:grid;grid-template-columns:1.45fr 1fr;gap:16px}
.panel{
 background:var(--panel);
 border:1px solid var(--line);
 border-radius:12px;
 overflow:hidden
}
.toolbar{
 min-height:52px;
 padding:10px 14px;
 border-bottom:1px solid var(--line);
 display:flex;
 align-items:center;
 gap:8px;
 flex-wrap:wrap
}
.toolbar b{margin-right:auto}
input{
 background:#090d13;
 border:1px solid #273142;
 color:white;
 border-radius:8px;
 padding:9px 11px;
 outline:none;
 min-width:180px
}
input:focus{border-color:#6d5b2a}
.action{
 border:1px solid #4e4325;
 background:#17140c;
 color:var(--gold2);
 border-radius:8px;
 padding:9px 12px
}
.action:hover{background:#211d10}
.action.blue{border-color:#29476d;color:#8ec2ff;background:#0d1724}
.action.danger{border-color:#6d2930;color:#ff8b94;background:#1b0d10}
table{width:100%;border-collapse:collapse}
th,td{
 padding:11px 13px;
 border-bottom:1px solid #18202c;
 text-align:left;
 font-size:12px;
 vertical-align:middle
}
th{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#687486;background:#0a0e14}
tr:hover td{background:#10151d}
.good{color:var(--green)}
.bad{color:var(--red)}
.warn{color:var(--orange)}
.blueText{color:var(--blue)}
.muted{color:var(--muted)}
.mono{font-family:Consolas,monospace;font-size:11px}
.empty{padding:35px;text-align:center;color:var(--muted)}
.loading{padding:35px;text-align:center;color:var(--muted)}
.error{
 margin:15px 0;
 border:1px solid #632832;
 background:#190c10;
 color:#ff9ba2;
 padding:13px;
 border-radius:9px;
 font-size:12px
}
.chart{
 height:290px;
 padding:20px 18px 14px;
 display:flex;
 flex-direction:column
}
.bars{
 flex:1;
 display:flex;
 align-items:flex-end;
 gap:7px;
 border-bottom:1px solid #27303d;
 padding:0 5px
}
.barWrap{
 flex:1;
 height:100%;
 display:flex;
 flex-direction:column;
 justify-content:flex-end;
 align-items:center;
 min-width:0
}
.bar{
 width:min(34px,75%);
 min-height:2px;
 border-radius:5px 5px 0 0;
 background:linear-gradient(180deg,var(--gold2),#8b7130);
 transition:.25s
}
.bar:hover{filter:brightness(1.2)}
.barLabel{
 margin-top:6px;
 color:#657183;
 font-size:9px;
 white-space:nowrap;
 transform:rotate(-35deg)
}
.barValue{font-size:8px;color:#9ba5b3;margin-bottom:4px;white-space:nowrap}
.chartLegend{font-size:10px;color:#697586;margin-top:13px}
.rank{padding:14px}
.rankRow{
 display:grid;
 grid-template-columns:30px 1fr auto;
 gap:10px;
 align-items:center;
 padding:10px 0;
 border-bottom:1px solid #19202b
}
.rankRow:last-child{border-bottom:0}
.rankN{color:var(--gold2);font-weight:700}
.rankName{min-width:0}
.rankName b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rankName span{font-size:10px;color:var(--muted)}
.progress{height:5px;background:#1b222d;border-radius:99px;margin-top:7px;overflow:hidden}
.progress i{display:block;height:100%;background:var(--gold);border-radius:99px}
.activity{padding:4px 15px}
.activityRow{
 display:grid;
 grid-template-columns:105px 1fr auto;
 gap:12px;
 padding:12px 0;
 border-bottom:1px solid #19202b;
 font-size:11px
}
.activityRow:last-child{border-bottom:0}
.statusDot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--green)}
.statLine{
 display:flex;
 justify-content:space-between;
 padding:11px 0;
 border-bottom:1px solid #19202b;
 font-size:12px
}
.statLine:last-child{border-bottom:0}
.notice{
 padding:13px 15px;
 border-left:3px solid var(--gold);
 background:#11110d;
 color:#b7bec9;
 font-size:12px
}
.modalBack{
 position:fixed;inset:0;background:rgba(0,0,0,.72);
 display:none;align-items:center;justify-content:center;
 z-index:100
}
.modal{
 width:min(760px,92vw);
 max-height:85vh;
 overflow:auto;
 background:#0d1118;
 border:1px solid #303949;
 border-radius:14px;
 box-shadow:0 30px 100px #000
}
.modalHead{
 padding:16px 18px;
 border-bottom:1px solid var(--line);
 display:flex;justify-content:space-between
}
.modalBody{padding:18px}
.close{
 background:none;border:0;color:#8994a3;font-size:24px
}
.mobileBtn{display:none}
@media(max-width:1100px){
 .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .two{grid-template-columns:1fr}
}
@media(max-width:760px){
 .side{transform:translateX(-100%);transition:.2s}
 .side.open{transform:translateX(0)}
 .main{margin-left:0;width:100%}
 .mobileBtn{display:block}
 .top{padding:13px 15px}
 #content{padding:15px}
 .grid{grid-template-columns:1fr}
}


.manage-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;padding:11px 15px;border:1px solid var(--line);border-radius:12px;background:linear-gradient(145deg,var(--panel),#0b0f15);color:var(--muted);font-size:13px}
#master-expansion-button{display:none!important}
.mcc-field .input,.mcc-field input,.mcc-field select{box-sizing:border-box}

</style>
<style id="master-company-control-style">
/* MASTER COMPANY CONTROL V1 */

/* MASTER COMPANY CONTROL V1 */

.mcc-overlay{
    position:fixed;
    inset:0;
    background:rgba(0,0,0,.72);
    z-index:99990;
    display:none;
    align-items:center;
    justify-content:center;
    padding:22px;
    backdrop-filter:blur(5px);
}

.mcc-overlay.open{
    display:flex;
}

.mcc-modal{
    width:min(1180px,96vw);
    max-height:92vh;
    overflow:auto;
    background:#0b0f14;
    border:1px solid rgba(255,255,255,.10);
    border-radius:16px;
    box-shadow:0 30px 100px rgba(0,0,0,.55);
}

.mcc-head{
    position:sticky;
    top:0;
    z-index:3;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:15px;
    padding:18px 20px;
    background:#0d1218;
    border-bottom:1px solid rgba(255,255,255,.08);
}

.mcc-title{
    font-size:18px;
    font-weight:800;
}

.mcc-subtitle{
    margin-top:3px;
    font-size:12px;
    opacity:.58;
}

.mcc-close{
    border:0;
    background:rgba(255,255,255,.07);
    color:inherit;
    width:36px;
    height:36px;
    border-radius:9px;
    cursor:pointer;
    font-size:18px;
}

.mcc-body{
    padding:20px;
}

.mcc-kpis{
    display:grid;
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:12px;
    margin-bottom:18px;
}

.mcc-kpi{
    padding:15px;
    border:1px solid rgba(255,255,255,.08);
    background:rgba(255,255,255,.025);
    border-radius:12px;
}

.mcc-kpi small{
    display:block;
    opacity:.55;
    font-size:11px;
    margin-bottom:6px;
}

.mcc-kpi strong{
    font-size:19px;
}

.mcc-grid{
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:14px;
}

.mcc-card{
    border:1px solid rgba(255,255,255,.08);
    background:rgba(255,255,255,.025);
    border-radius:13px;
    padding:16px;
}

.mcc-card.full{
    grid-column:1/-1;
}

.mcc-card h3{
    margin:0 0 12px;
    font-size:14px;
}

.mcc-fields{
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:10px;
}

.mcc-field label{
    display:block;
    font-size:11px;
    opacity:.58;
    margin-bottom:5px;
}

.mcc-field input,
.mcc-field select{
    width:100%;
    box-sizing:border-box;
}

.mcc-actions{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
}

.mcc-btn{
    border:1px solid rgba(255,255,255,.10);
    background:rgba(255,255,255,.06);
    color:inherit;
    border-radius:8px;
    padding:8px 11px;
    cursor:pointer;
    font-size:12px;
}

.mcc-btn:hover{
    background:rgba(255,255,255,.11);
}

.mcc-btn.primary{
    background:rgba(255,193,7,.13);
    border-color:rgba(255,193,7,.35);
}

.mcc-btn.danger{
    background:rgba(255,70,70,.10);
    border-color:rgba(255,70,70,.28);
}

.mcc-btn.success{
    background:rgba(70,210,130,.10);
    border-color:rgba(70,210,130,.28);
}

.mcc-status{
    display:inline-flex;
    padding:5px 8px;
    border-radius:7px;
    font-size:11px;
    background:rgba(255,255,255,.07);
}

.mcc-log{
    min-height:50px;
    max-height:220px;
    overflow:auto;
    padding:10px;
    background:#070a0e;
    border-radius:9px;
    font-family:monospace;
    font-size:11px;
    white-space:pre-wrap;
}

@media(max-width:850px){
    .mcc-kpis{
        grid-template-columns:repeat(2,minmax(0,1fr));
    }

    .mcc-grid{
        grid-template-columns:1fr;
    }

    .mcc-card.full{
        grid-column:auto;
    }
}

@media(max-width:560px){
    .mcc-fields{
        grid-template-columns:1fr;
    }

    .mcc-body{
        padding:12px;
    }

    .mcc-kpis{
        grid-template-columns:1fr 1fr;
    }
}

</style>
<style id="master-command-center-premium">
/* Command Center: camada de apresentação do MASTER existente. */
:root{
 --bg:#0a0c10;--panel:#11151b;--panel2:#151a21;--line:#252b34;
 --text:#edf0f4;--muted:#929ba8;--gold:#c7a45a;--gold2:#e6cb86;
 --green:#68c995;--red:#e7797e;--orange:#dfae63;--blue:#7daee5;
 color-scheme:dark;font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif
}
html,body{min-height:100%;background:var(--bg);color:var(--text);font-size:13px;-webkit-font-smoothing:antialiased}
body{background-image:radial-gradient(ellipse at 76% -18%,rgba(199,164,90,.075),transparent 38%),linear-gradient(90deg,transparent 49.95%,rgba(255,255,255,.012) 50%,transparent 50.05%)}
.app{min-height:100vh}
.side{width:252px;background:linear-gradient(180deg,#101319 0%,#0c0f14 100%);border-right:1px solid #272b31;box-shadow:12px 0 34px rgba(0,0,0,.12);scrollbar-width:thin;scrollbar-color:#34383e transparent}
.brand{position:relative;padding:26px 21px 23px 24px;border-bottom:1px solid #ffffff0d}
.brand:before{content:"";position:absolute;left:0;top:22px;bottom:22px;width:2px;background:linear-gradient(180deg,var(--gold2),#8d7135);box-shadow:0 0 13px #c7a45a44}
.brand b{font-size:15px;letter-spacing:.16em;color:#f2e7c8;font-weight:750}
.brand span{font-size:9px;letter-spacing:.17em;color:#8d929a;margin-top:8px}
.nav{padding:10px 12px 20px}
.nav .section-label{padding:18px 10px 7px;color:#666d77;font-size:9px;font-weight:700;letter-spacing:.16em}
.nav button{position:relative;min-height:39px;padding:9px 11px;gap:11px;margin:2px 0;border-radius:7px;color:#aab0ba;font-size:12px;letter-spacing:.005em}
.nav button:hover{background:#ffffff08;color:#f1f2f3}
.nav button.active{background:linear-gradient(90deg,#c7a45a1b,#c7a45a08);color:#f0d995;box-shadow:inset 2px 0 0 var(--gold),inset 0 0 0 1px #c7a45a12}
.nav button.active:after{content:"";position:absolute;right:11px;width:5px;height:5px;border-radius:50%;background:var(--gold2);box-shadow:0 0 9px #e6cb8680}
.main{margin-left:252px;width:calc(100% - 252px);background:transparent}
.top{min-height:77px;padding:13px clamp(20px,3vw,42px);background:rgba(10,12,16,.84);border-bottom-color:#ffffff10;backdrop-filter:blur(18px)}
.title h1{font-size:20px;font-weight:650;letter-spacing:-.035em;color:#f4f2ed}
.title p{font-size:11px;margin-top:5px;color:#8b929d}
.pill{padding:7px 11px;border-color:#9d824648;background:#c7a45a0c;color:#dfc783;font-size:9px;font-weight:700;letter-spacing:.13em}
.mobileBtn{border-color:#343a43!important;background:#151920!important;color:#d8dce2!important;border-radius:7px!important}
#content{max-width:1720px;margin:0 auto;padding:clamp(18px,2.4vw,34px) clamp(16px,2.8vw,42px) 44px}
.grid{gap:11px}
.card{position:relative;background:linear-gradient(145deg,#14181f,#101319 80%);border-color:#292f38;border-radius:9px;padding:16px 17px;box-shadow:0 8px 24px #00000012;transition:border-color .16s,transform .16s,background .16s}
.card:hover{border-color:#45413a;transform:translateY(-1px)}
.card .k{font-size:9px;font-weight:700;letter-spacing:.13em;color:#8e96a1}
.card .v{font-size:clamp(21px,2vw,27px);font-variant-numeric:tabular-nums;letter-spacing:-.04em;color:#f0eee9;margin-top:11px}
.card .s{font-size:10px;color:#747d88;margin-top:7px}
.section{margin-top:14px}
.two{gap:13px;grid-template-columns:minmax(0,1.45fr) minmax(290px,1fr)}
.panel{background:#11151b;border-color:#292f38;border-radius:9px;box-shadow:0 9px 28px #00000012}
.toolbar{min-height:49px;padding:9px 13px;border-bottom-color:#252b33;color:#e8e9eb}
.toolbar b{font-size:12px;font-weight:650;letter-spacing:.01em}
input,select,textarea{background:#0c1015;border:1px solid #303640;color:#e9ebee;border-radius:6px;padding:9px 11px;min-width:0;transition:border-color .15s,box-shadow .15s,background .15s}
input::placeholder,textarea::placeholder{color:#69717c}
input:focus,select:focus,textarea:focus{border-color:#a98c4d;box-shadow:0 0 0 3px #c7a45a16;outline:0;background:#0f1319}
.action,.mcc-btn{border:1px solid #39352a;background:#19170f;color:#dfc783;border-radius:6px;padding:8px 11px;font-size:11px;font-weight:620;transition:background .15s,border-color .15s,color .15s,transform .15s,box-shadow .15s}
.action:hover,.mcc-btn:hover{background:#242014;border-color:#74603a;color:#f3dda1;transform:translateY(-1px)}
.action.blue{border-color:#30445b;background:#111a25;color:#9bc3ed}
.action.danger,.mcc-btn.danger{border-color:#593137;background:#1b1114;color:#e99498}
table{min-width:0;border-collapse:collapse}
th,td{padding:11px 13px;border-bottom-color:#222831;font-size:11px;line-height:1.45}
th{background:#0e1217;color:#818a96;font-size:9px;font-weight:700;letter-spacing:.11em;white-space:nowrap}
tr:hover td{background:#ffffff04}
td:first-child{color:#e8e8e6}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--orange)}.blueText{color:var(--blue)}.muted{color:#929ba8}
.mono{color:#b9c0ca}
.empty,.loading{padding:29px 18px;color:#89919d;font-size:12px}
.loading:before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;border-radius:50%;background:var(--gold);box-shadow:0 0 10px #c7a45a70;animation:masterPulse 1.15s ease-in-out infinite}
@keyframes masterPulse{50%{opacity:.35;box-shadow:0 0 2px #c7a45a20}}
.error{border-color:#63353a;background:#1a1013;color:#ef9a9e;border-radius:7px}
.chart{height:280px;padding:19px 17px 14px}
.bars{border-bottom-color:#34383b}
.bar{width:min(30px,72%);border-radius:3px 3px 0 0;background:linear-gradient(180deg,#e5cb86,#9f8040);box-shadow:0 0 13px #c7a45a16}
.barLabel{font-size:9px;color:#8b929a}
.barValue{font-size:9px;color:#c5c9ce}
.chartLegend{color:#8a929d;font-size:10px}
.rank{padding:12px 15px}
.rankRow,.activityRow,.statLine{border-bottom-color:#242a32}
.rankN{color:#dfc783}.rankName span{color:#858e99}
.progress{height:4px;background:#292e35}.progress i{background:linear-gradient(90deg,#9b7a39,#dfc783)}
.activity{padding:4px 15px}.activityRow{font-size:10px;color:#c1c5cc}
.statusDot{width:6px;height:6px;box-shadow:0 0 8px #68c99566}
.notice{border:1px solid #5b4c2d;border-left:2px solid var(--gold);border-radius:0 6px 6px 0;background:#17150f;color:#c8bc9a;padding:12px 14px;font-size:11px}
.manage-bar{border-color:#2b3038;border-radius:8px;background:#12161c;color:#a0a7b1}
.modalBack{background:#030405c9;backdrop-filter:blur(7px);padding:16px}
.modal{width:min(820px,94vw);max-height:min(88dvh,900px);background:#11151b;border-color:#343a44;border-radius:12px;box-shadow:0 30px 100px #000b}
.modalHead{position:sticky;top:0;z-index:2;background:#12161c;border-bottom-color:#292f38;padding:15px 18px;color:#f0eee9}
.modalBody{padding:18px}
.close{width:34px;height:34px;border-radius:6px;background:#ffffff08;color:#aab1bb;font-size:21px}
.close:hover{background:#ffffff12;color:#fff}
.mcc-overlay{background:#030405d1;backdrop-filter:blur(8px);padding:clamp(10px,3vw,28px)}
.mcc-modal{width:min(1180px,96vw);max-height:92dvh;background:#10141a;border:1px solid #343a43;border-radius:12px;box-shadow:0 30px 100px #000b}
.mcc-head{background:#12161c;border-bottom-color:#292f38;padding:16px 19px}
.mcc-title{font-size:16px;color:#f0eee9}.mcc-subtitle{color:#9ba2ac;opacity:1;font-size:10px;letter-spacing:.06em}
.mcc-close{background:#ffffff0a;color:#bdc2ca;border-radius:7px}.mcc-close:hover{background:#ffffff14}
.mcc-body{padding:18px}
.mcc-kpis{gap:9px;margin-bottom:13px}
.mcc-kpi,.mcc-card{border:1px solid #292f38;background:linear-gradient(145deg,#151a21,#11151b);border-radius:8px}
.mcc-kpi{padding:13px}.mcc-kpi small{color:#939ba6;opacity:1;font-size:9px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.mcc-kpi strong{font-size:19px;color:#eeeae1;font-variant-numeric:tabular-nums}
.mcc-grid{gap:11px}.mcc-card{padding:14px}.mcc-card h3{font-size:12px;color:#e7e8e9}
.mcc-field label{color:#9aa2ad;opacity:1;font-size:10px}
.mcc-field input,.mcc-field select{width:100%;min-height:38px}
.mcc-btn.primary{background:#211c10;border-color:#6c5831;color:#e5ca83}.mcc-btn.success{background:#101b16;border-color:#345744;color:#88c5a1}
.mcc-status{border:1px solid #343a42;background:#191e25;color:#c7cbd1;border-radius:5px}
.mcc-log{background:#0a0d11;border:1px solid #242a32;color:#c4c9d0;border-radius:7px}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #dfc783;outline-offset:2px}
@media(max-width:1100px){.side{width:226px}.main{margin-left:226px;width:calc(100% - 226px)}.two{grid-template-columns:minmax(0,1.2fr) minmax(260px,1fr)}#content{padding:22px}.grid{gap:9px}}
@media(max-width:760px){.side{width:min(290px,86vw);background:#10141a;box-shadow:20px 0 65px #0009}.main{margin-left:0;width:100%}.top{min-height:68px;padding:10px 13px;gap:8px}.title h1{font-size:17px}.title p{font-size:10px}.pill{font-size:8px;padding:7px 8px}#content{padding:15px 12px 28px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.card{padding:13px}.card .v{font-size:21px}.two{grid-template-columns:minmax(0,1fr);gap:10px}.panel{min-width:0}.toolbar{align-items:stretch}.toolbar input,.toolbar select{width:100%;max-width:100%;min-width:0}.toolbar .action{min-height:36px}.chart{height:245px;padding:14px 10px 12px}.activityRow{grid-template-columns:minmax(68px,auto) 1fr auto;gap:7px}.modalBack{padding:0;align-items:flex-end}.modal{width:100%;max-height:calc(100dvh - 12px);border-radius:12px 12px 0 0}.modalBody{padding:14px}.mcc-overlay{padding:0;align-items:flex-end}.mcc-modal{width:100%;max-height:calc(100dvh - 8px);border-radius:13px 13px 0 0}.mcc-head{padding:13px 15px}.mcc-body{padding:12px}.mcc-kpis{gap:7px}.mcc-kpi{padding:11px}.mcc-grid{gap:8px}}
@media(max-width:420px){#content{padding:12px 9px 24px}.grid{gap:7px}.card{padding:11px 10px}.card .k{font-size:8px;letter-spacing:.09em}.card .v{font-size:19px;margin-top:8px}.card .s{font-size:9px}.top{padding-inline:10px}.title h1{font-size:15px}.title p{max-width:26ch;line-height:1.35}.pill{padding:6px;font-size:7px;letter-spacing:.08em}.nav{padding-inline:9px}.nav button{min-height:40px}.toolbar{padding:9px}.mcc-kpi strong{font-size:17px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}
/* Refinamento final de tipografia e hierarquia, preservando os componentes e dados. */
html,body{font-family:Inter,"Segoe UI Variable","Segoe UI",system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5;text-rendering:optimizeLegibility}
body{font-weight:400}
.brand b{font-size:14px;font-weight:650;letter-spacing:.11em}
.brand span{font-size:10px;font-weight:450;letter-spacing:.09em;color:#a0a6af}
.nav .section-label{font-size:10px;font-weight:600;letter-spacing:.09em;color:#858c96}
.nav button{font-size:13px;font-weight:450;line-height:1.4;letter-spacing:0}
.title h1{font-size:21px;font-weight:600;line-height:1.25;letter-spacing:-.025em;color:#f0efec}
.title p{font-size:12px;font-weight:400;line-height:1.45;color:#a0a7b1}
.pill{font-size:10px;font-weight:600;letter-spacing:.08em;line-height:1.35}
.card .k{font-size:10px;font-weight:550;letter-spacing:.07em;line-height:1.4;color:#a2a9b2}
.card .v{font-size:clamp(22px,2vw,27px);font-weight:600;line-height:1.2;letter-spacing:-.025em;font-variant-numeric:tabular-nums lining-nums}
.card .s{font-size:11px;font-weight:400;line-height:1.45;color:#969eaa}
.toolbar{line-height:1.45}
.toolbar b{font-size:13px;font-weight:600;line-height:1.35;letter-spacing:0}
input,select,textarea{font-family:inherit;font-size:13px;font-weight:400;line-height:1.45}
input::placeholder,textarea::placeholder{color:#929aa5;opacity:1}
.action,.mcc-btn{font-family:inherit;font-size:12px;font-weight:550;line-height:1.35;letter-spacing:0}
th,td{font-size:12px;line-height:1.55;font-variant-numeric:tabular-nums}
th{font-size:10px;font-weight:600;letter-spacing:.065em;color:#9aa2ad}
td{font-weight:400;color:#d5d8dd}
td:first-child{color:#e6e6e3;font-weight:500}
.mono{font-size:11px;line-height:1.5}
.empty,.loading{font-size:13px;line-height:1.55}
.barLabel{font-size:10px;line-height:1.3;letter-spacing:0;transform:rotate(-30deg)}
.barValue{font-size:10px;font-weight:500;line-height:1.3}
.chartLegend{font-size:11px;line-height:1.45}
.rankN{font-weight:600}.rankName b{font-size:12px;font-weight:550;line-height:1.4}.rankName span{font-size:11px;line-height:1.45;color:#a0a7b1}
.activityRow{font-size:12px;line-height:1.5;color:#c9cdd3}
.statLine{font-size:12px;line-height:1.5}
.notice{font-size:12px;line-height:1.55;color:#d0c7ae}
.manage-bar{font-size:12px;line-height:1.5}
.modalHead{font-size:14px;line-height:1.4}.modalBody{font-size:13px;line-height:1.55}
.mcc-title{font-size:17px;font-weight:600;line-height:1.3;letter-spacing:-.015em}
.mcc-subtitle{font-size:11px;font-weight:400;line-height:1.45;letter-spacing:.015em;color:#a0a7b1}
.mcc-kpi small{font-size:10px;font-weight:550;line-height:1.4;letter-spacing:.055em;text-transform:none;color:#a2a9b2}
.mcc-kpi strong{font-size:20px;font-weight:600;line-height:1.25;letter-spacing:-.02em;font-variant-numeric:tabular-nums lining-nums}
.mcc-card h3{font-size:13px;font-weight:600;line-height:1.4}
.mcc-field label{font-size:11px;font-weight:500;line-height:1.4;color:#a7aeb8}
.mcc-status{font-size:11px;font-weight:500;line-height:1.35}
@media(max-width:760px){.title h1{font-size:18px}.title p{font-size:11px}.card .k{font-size:10px}.card .s{font-size:10px}th,td{font-size:12px}.mcc-kpi small{font-size:10px}}
@media(max-width:420px){.title h1{font-size:16px}.title p{font-size:11px;line-height:1.4}.pill{font-size:9px;letter-spacing:.045em}.card .v{font-size:20px}.card .k{font-size:9px}.card .s{font-size:10px}.nav button{font-size:13px}.toolbar b{font-size:12px}}
/* NEXORA MASTER — refinamento visual da experiência já existente. */
:root{--bg:#080D1D;--panel:#0D1428;--panel2:#141C37;--line:#25304A;--text:#F8FAFC;--muted:#94A3B8;--gold:#A855F7;--gold2:#C084FC;--green:#2DD4BF;--red:#F87171;--orange:#22D3EE}
*{font-family:Inter,Manrope,"Segoe UI",system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:#E8EDF5;font-size:13px;line-height:1.5}
.app{background:var(--bg)}.side{background:#10172B;border-right-color:#202c45;box-shadow:8px 0 26px #02061733}
.brand b{color:#F8FAFC;font-size:15px;font-weight:650;letter-spacing:.08em}.brand span{color:#94A3B8;font-size:11px;letter-spacing:0}
.section-label{color:#71809A;font-size:10px;letter-spacing:.08em}.nav button{color:#AAB6C9;border-radius:6px}.nav button:hover{background:#18223B}.nav button.active{background:#1B2540;color:#F8FAFC}.nav button.active::before{background:#A855F7}
.main{background:#080D1D}.top{background:#0D1428;border-color:#202c45}.title h1{font-weight:620;color:#F8FAFC;letter-spacing:-.025em}.title p,.muted{color:#94A3B8}.pill{border-color:#34415E;background:#111a30;color:#C7D2E2}
#content{max-width:1680px}.card,.panel,.modal,.manage-bar{border-color:#25304A;background:#111a30;box-shadow:none}.card{border-radius:8px}.card .k{color:#A5B1C5;font-size:11px;font-weight:500;letter-spacing:0;text-transform:none}.card .v{color:#F8FAFC;font-size:clamp(21px,1.7vw,26px);font-weight:600}.card .s{color:#8492A8;font-size:11px}.toolbar b{color:#E8EDF5;font-weight:600}.action,.mcc-btn{border-color:#34415E;border-radius:6px;background:#17223A;color:#DCE4EF}.action:hover,.mcc-btn:hover{border-color:#596986;background:#1d2b49}.action.primary,.mcc-btn.primary{border-color:#9E64DF;background:#8140C4;color:#fff}
input,select,textarea{border-color:#34415E!important;border-radius:6px!important;background:#0A1122!important;color:#F8FAFC!important}input::placeholder,textarea::placeholder{color:#71809A!important}input:focus,select:focus,textarea:focus{border-color:#A855F7!important;outline:2px solid #A855F733}
table{border-color:#202c45}th{background:#111a30;color:#A5B1C5;font-size:10px;letter-spacing:.04em}td{border-color:#202c45;color:#DCE4EF}td:first-child{color:#F0F3F8}tbody tr:hover{background:#151f36}.pill.good,.good{color:#5EEAD4}.warn{color:#FCD34D}.danger,.bad{color:#FCA5A5}
.modalBack{background:#030713dd}.modal{border-radius:10px}.close{color:#A5B1C5}.empty{color:#A5B1C5;padding:22px 14px;text-align:center}.notice{border-color:#34415E;background:#141C37;color:#B6C3D6}
.grid{gap:12px}.two{gap:14px}.chart{height:auto;min-height:240px;padding:16px}.nexora-line-chart{width:100%;overflow:hidden}.nexora-line-chart svg{display:block;width:100%;height:auto;min-height:200px}.nexora-chart-grid{stroke:#25304A;stroke-width:1}.nexora-chart-line{fill:none;stroke:#A855F7;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.nexora-chart-dot{fill:#22D3EE;stroke:#0D1428;stroke-width:2}.nexora-chart-label{fill:#94A3B8;font-size:11px;font-family:Inter,system-ui,sans-serif}
.nexora-segment{display:flex;align-items:center;justify-content:center;gap:28px;min-height:164px;padding:18px}.nexora-donut{display:grid;place-items:center;width:124px;height:124px;flex:none;border-radius:50%;background:conic-gradient(#A855F7 0 var(--loja),#22D3EE var(--loja) 100%);position:relative}.nexora-donut::before{content:"";position:absolute;inset:12px;border-radius:50%;background:#111a30}.nexora-donut span{position:relative;color:#F8FAFC;font-size:24px;font-weight:600}.nexora-legend{display:grid;gap:13px;min-width:130px}.nexora-legend div{display:grid;grid-template-columns:10px 1fr auto;align-items:center;gap:8px;color:#A5B1C5;font-size:12px}.nexora-legend i{width:8px;height:8px;border-radius:50%;background:#A855F7}.nexora-legend i.restaurante{background:#22D3EE}.nexora-legend b{color:#F8FAFC;font-weight:550}
@media(max-width:760px){.toolbar{flex-wrap:wrap}.toolbar>.muted{flex:1 1 100%;text-align:left}.nexora-segment{min-height:148px;gap:20px}.nexora-donut{width:108px;height:108px}.nexora-chart-label{font-size:10px}}
@media(max-width:420px){.nexora-segment{gap:16px;padding:14px}.nexora-donut{width:94px;height:94px}.nexora-donut::before{inset:10px}.nexora-donut span{font-size:20px}.nexora-legend{min-width:115px}.nexora-legend div{font-size:11px}.nexora-chart-label{font-size:9px}}
</style>
</head>
<body>


<div class="app">

<aside class="side" id="sidebar">

<div class="brand">
<b>NEXORA MASTER</b>
<span>Enterprise Management Platform</span>
</div>

<nav class="nav" id="nav">

<div class="section-label">Visão geral</div>

<button data-page="dashboard">▣ <span>Visão geral</span></button>
<button data-page="companies">▦ <span>Empresas</span></button>
<button data-page="users">♙ <span>Usuários</span></button>

<div class="section-label">Operação</div>

<button data-page="operations">◈ <span>Operações</span></button>
<button data-page="finance">◉ <span>Financeiro</span></button>
<button data-page="billing">▤ <span>Planos e cobrança</span></button>
<button data-page="analytics">◫ <span>Analytics</span></button>

<div class="section-label">Controle</div>

<button data-page="security">◉ <span>Segurança</span></button>
<button data-page="audit">≡ <span>Auditoria</span></button>
<button data-page="alerts">⚠ <span>Alertas</span></button>
<button data-page="search">⌕ <span>Busca global</span></button>

<div class="section-label">Infraestrutura</div>

<button data-page="diagnostics">✓ <span>Diagnóstico</span></button>
<button data-page="system">⚙ <span>Sistema</span></button>
<button data-page="data">▤ <span>Data Center</span></button>
<button data-page="backups">↥ <span>Backups</span></button>
<button data-page="maintenance">⚒ <span>Manutenção</span></button>

<div class="section-label">Plataforma</div>

<button data-page="flags">◇ <span>Feature Flags</span></button>
<button data-page="api">⌘ <span>API / Integrações</span></button>

<div class="section-label">Sessão</div>

<button onclick="goStore()">← <span>Sistema da loja</span></button>
<button onclick="logoutMaster()">⏻ <span>Encerrar MASTER</span></button>


<button onclick="openMasterAdvancedActions()">⚙ <span>Ações avançadas</span></button>
</nav>
</aside>

<main class="main">

<div class="top">
<div style="display:flex;align-items:center;gap:12px">
<button class="action mobileBtn" onclick="toggleSide()">☰</button>
<div class="title">
<h1 id="pageTitle">Central de Controle NEXORA</h1>
<p id="pageDesc">Visão global da plataforma</p>
</div>
</div>
<div class="pill">MASTER · PROTEGIDO</div>
</div>

<div id="content"></div>

</main>
</div>

<div class="modalBack" id="modalBack">
<div class="modal">
<div class="modalHead">
<b id="modalTitle">Detalhes</b>
<button class="close" onclick="closeModal()">×</button>
</div>
<div class="modalBody" id="modalBody"></div>
</div>
</div>


<!-- ===== COMPANY CONTROL V1 (dentro de EMPRESAS) ===== -->
<!-- MASTER COMPANY CONTROL V1 -->

<div id="mcc-overlay" class="mcc-overlay" onclick="if(event.target===this)closeMasterCompany()">
  <div class="mcc-modal">

    <div class="mcc-head">
      <div>
        <div class="mcc-title" id="mcc-title">Administração da empresa</div>
        <div class="mcc-subtitle" id="mcc-subtitle">MASTER CONTROL</div>
      </div>
      <button class="mcc-close" onclick="closeMasterCompany()">×</button>
    </div>

    <div class="mcc-body">

      <div class="mcc-kpis">
        <div class="mcc-kpi">
          <small>Usuários</small>
          <strong id="mcc-users">—</strong>
        </div>

        <div class="mcc-kpi">
          <small>Produtos</small>
          <strong id="mcc-products">—</strong>
        </div>

        <div class="mcc-kpi">
          <small>Clientes</small>
          <strong id="mcc-customers">—</strong>
        </div>

        <div class="mcc-kpi">
          <small>Vendas</small>
          <strong id="mcc-sales">—</strong>
        </div>
      </div>

      <div class="mcc-grid">

        <div class="mcc-card">
          <h3>Controle da empresa</h3>

          <div class="mcc-actions">
            <button class="mcc-btn primary" onclick="mccEditName()">Editar nome</button>
            <button class="mcc-btn success" onclick="mccActivate()">Ativar</button>
            <button class="mcc-btn danger" onclick="mccSuspend()">Suspender</button>
            <button class="mcc-btn danger" onclick="mccRevokeSessions()">Encerrar sessões</button>
          </div>
        </div>

        <div class="mcc-card">
          <h3>Administrador</h3>

          <div class="mcc-actions">
            <button class="mcc-btn primary" onclick="mccCreateAdmin()">+ Criar administrador</button>
            <button class="mcc-btn" onclick="mccOpenUsers()">Gerenciar usuários</button>
          </div>
        </div>

        <div class="mcc-card">
          <h3>Plano e cobrança</h3>

          <div class="mcc-fields">

            <div class="mcc-field">
              <label>Plano</label>
              <input id="mcc-plan" class="input" placeholder="standard">
            </div>

            <div class="mcc-field">
              <label>Mensalidade</label>
              <input id="mcc-price" class="input" type="number" step="0.01" placeholder="0.00">
            </div>

            <div class="mcc-field">
              <label>Status da cobrança</label>
              <select id="mcc-billing-status" class="input">
                <option value="active">Ativa</option>
                <option value="pending">Pendente</option>
                <option value="paid">Paga</option>
                <option value="overdue">Atrasada</option>
                <option value="cancelled">Cancelada</option>
              </select>
            </div>

          </div>

          <div class="mcc-actions" style="margin-top:12px">
            <button class="mcc-btn primary" onclick="mccSavePlan()">Salvar plano</button>
            <button class="mcc-btn" onclick="mccRegisterCharge()">Registrar cobrança</button>
          </div>
        </div>

        <div class="mcc-card">
          <h3>Limites da empresa</h3>

          <div class="mcc-fields">

            <div class="mcc-field">
              <label>Máx. usuários</label>
              <input id="mcc-max-users" class="input" type="number" min="0">
            </div>

            <div class="mcc-field">
              <label>Máx. produtos</label>
              <input id="mcc-max-products" class="input" type="number" min="0">
            </div>

            <div class="mcc-field">
              <label>Máx. clientes</label>
              <input id="mcc-max-customers" class="input" type="number" min="0">
            </div>

            <div class="mcc-field">
              <label>Máx. vendas/mês</label>
              <input id="mcc-max-sales" class="input" type="number" min="0">
            </div>

            <div class="mcc-field">
              <label>Armazenamento MB</label>
              <input id="mcc-max-storage" class="input" type="number" min="0">
            </div>

          </div>

          <div class="mcc-actions" style="margin-top:12px">
            <button class="mcc-btn primary" onclick="mccSaveLimits()">Salvar limites</button>
          </div>
        </div>

        <div class="mcc-card">
          <h3>Suporte MASTER</h3>

          <div id="mcc-support-status" class="mcc-status">
            Verificando...
          </div>

          <div class="mcc-actions" style="margin-top:12px">
            <button class="mcc-btn primary" onclick="mccStartSupport()">Entrar em suporte</button>
            <button class="mcc-btn danger" onclick="mccStopSupport()">Encerrar suporte</button>
            <button class="mcc-btn" onclick="mccSnapshot()">Atualizar snapshot</button>
          </div>
        </div>

        <div class="mcc-card full">
          <h3>Snapshot da empresa</h3>

          <div id="mcc-snapshot" class="mcc-log">
            Carregando...
          </div>
        </div>

        <div class="mcc-card full">
          <h3>Log da operação</h3>

          <div id="mcc-log" class="mcc-log">
            Pronto.
          </div>
        </div>

      </div>
    </div>
  </div>
</div>


<script>
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const state = {
 page: 'dashboard',
 cache: {}
};

let masterRealtimeSocket=null;
let masterRealtimeTimer=null;
let masterFallbackTimer=null;
let masterRealtimeDelay=1000;
let masterRealtimeSeen=false;
let masterRefreshTimer=null;
let masterRefreshBusy=false;
let masterRefreshDeferred=false;
let masterPendingEntities=new Set();

function masterHasOpenModal(){
 return $('#modalBack')?.style.display==='flex' || !!$('#mcc-overlay.open') || ($('#master-expansion-overlay')?.style.display==='flex');
}
function masterRealtimeConnect(){
 if(!window.WebSocket||document.hidden||masterRealtimeSocket?.readyState===WebSocket.OPEN||masterRealtimeSocket?.readyState===WebSocket.CONNECTING)return;
 try{
   const protocol=location.protocol==='https:'?'wss':'ws';
   const socket=new WebSocket(`${protocol}://${location.host}/ws`);
   masterRealtimeSocket=socket;
   socket.onopen=()=>{
     const wasConnected=masterRealtimeSeen;masterRealtimeSeen=true;masterRealtimeDelay=1000;clearTimeout(masterRealtimeTimer);
     if(wasConnected)masterRealtimeReceive({type:'admin_changed',entity:'administration'});
   };
   socket.onmessage=event=>{
     try{const data=JSON.parse(event.data||'{}');if(data.type==='admin_changed'&&['platform_activity','administration'].includes(data.entity))masterRealtimeReceive(data)}catch(e){console.debug('Evento MASTER invÃ¡lido',e)}
   };
   socket.onclose=()=>{
     if(masterRealtimeSocket===socket)masterRealtimeSocket=null;
     const delay=Math.min(30000,masterRealtimeDelay)*(0.8+Math.random()*0.4);masterRealtimeDelay=Math.min(30000,masterRealtimeDelay*2);
     clearTimeout(masterRealtimeTimer);masterRealtimeTimer=setTimeout(masterRealtimeConnect,delay);
   };
   socket.onerror=()=>{try{socket.close()}catch(e){}};
 }catch(e){const delay=Math.min(30000,masterRealtimeDelay);masterRealtimeDelay=Math.min(30000,masterRealtimeDelay*2);masterRealtimeTimer=setTimeout(masterRealtimeConnect,delay)}
}
function masterRealtimeReceive(data){
 if(!['platform_activity','administration'].includes(data.entity))return;
 masterPendingEntities.add(data.entity);clearTimeout(masterRefreshTimer);masterRefreshTimer=setTimeout(masterRefreshCurrent,250);
}
async function masterRefreshCurrent(){
 if(document.hidden)return;
 if(masterHasOpenModal()){masterRefreshDeferred=true;return}
 if(masterRefreshBusy)return;
 const page=state.page,fn=pages[page];if(!fn)return;
 const content=$('#content'),scrollTop=content?.scrollTop||0,windowY=window.scrollY;
 const filter=content?.querySelector('input[placeholder*="Pesquisar"]');
 const filterValue=filter?.value||'';
 const filterId=page==='companies'?'companyTable':page==='users'?'userTable':'';
 masterPendingEntities.clear();masterRefreshBusy=true;
 try{
   await fn();decoratePage(page);
   if(filterValue&&filterId){const refreshed=content.querySelector('input[placeholder*="Pesquisar"]');if(refreshed){refreshed.value=filterValue;filterTable(filterValue,filterId)}}
   if(content)content.scrollTop=scrollTop;window.scrollTo(0,windowY);
 }catch(e){console.debug('Falha na atualizaÃ§Ã£o realtime do MASTER:',e)}
 finally{masterRefreshBusy=false;if(masterPendingEntities.size)masterRefreshTimer=setTimeout(masterRefreshCurrent,250)}
}
function masterRealtimeModalClosed(){if(masterRefreshDeferred&&!masterHasOpenModal()){masterRefreshDeferred=false;masterRefreshTimer=setTimeout(masterRefreshCurrent,80)}}

function esc(v){
 return String(v ?? '')
   .replace(/&/g,'&amp;')
   .replace(/</g,'&lt;')
   .replace(/>/g,'&gt;')
   .replace(/"/g,'&quot;')
   .replace(/'/g,'&#039;');
}

function money(v){
 const n = Number(v || 0);
 return n.toLocaleString('pt-BR',{
   style:'currency',
   currency:'BRL'
 });
}

function number(v){
 return Number(v || 0).toLocaleString('pt-BR');
}

async function api(url, options = {}){
 const opts = Object.assign({
   credentials:'same-origin',
   headers:{'Content-Type':'application/json'}
 }, options);

 const response = await fetch(url, opts);

 const text = await response.text();

 let data = null;

 try{
   data = text ? JSON.parse(text) : {};
 }catch(e){
   throw new Error('Resposta inválida do servidor em ' + url);
 }

 if(!response.ok){
   throw new Error(data.detail || data.message || ('HTTP ' + response.status));
 }

 return data;
}

function loading(message='Carregando dados...'){
 $('#content').innerHTML = '<div class="loading">'+esc(message)+'</div>';
}

function errorBox(error){
 $('#content').innerHTML =
 '<div class="error"><b>Não foi possível carregar esta seção.</b><br><br>'+
 esc(error?.message || error) +
 '</div>';
}

function cards(items){
 return '<div class="grid">'+items.map(x =>
   '<div class="card">'+
   '<div class="k">'+esc(x[0])+'</div>'+
   '<div class="v">'+esc(x[1])+'</div>'+
   '<div class="s">'+esc(x[2] || '')+'</div>'+
   '</div>'
 ).join('')+'</div>';
}

function setPage(title,desc){
 $('#pageTitle').textContent = title;
 $('#pageDesc').textContent = desc;
}

function activate(page){
 $$('.nav button[data-page]').forEach(b=>{
   b.classList.toggle('active', b.dataset.page === page);
 });
}

function show(page){
 state.page = page;
 activate(page);
 if(window.innerWidth < 760) $('#sidebar').classList.remove('open');

 const fn = pages[page];

 if(!fn){
   errorBox(new Error('Seção não encontrada.'));
   return;
 }

 fn().then(()=>decoratePage(page)).catch(errorBox);
}

function toggleSide(){
 $('#sidebar').classList.toggle('open');
}

function openModal(title,html){
 $('#modalTitle').textContent = title;
 $('#modalBody').innerHTML = html;
 $('#modalBack').style.display = 'flex';
}

function closeModal(){
 $('#modalBack').style.display = 'none';
 masterRealtimeModalClosed();
}

async function loadEvolution(){
 try{
   return await api('/master/api/evolution');
 }catch(e){
   return null;
 }
}

function extractEvolution(data){
 if(!data) return [];

 let rows =
   data.months ||
   data.rows ||
   data.data ||
   data.evolution ||
   data.items ||
   data.history ||
   [];

 if(Array.isArray(data)) rows = data;

 if(!Array.isArray(rows)) return [];

 return rows.map(x=>{
   if(typeof x === 'number'){
     return {label:'',value:x};
   }

   return {
     label:x.label || x.month || x.date || x.day || x.period || x.created_at || '',
     value:Number(
       x.value ??
       x.revenue ??
       x.sales ??
       x.total ??
       x.amount ??
       0
     )
   };
 });
}

function chartHtml(rows){
 const valid=(rows||[]).filter(x=>Number.isFinite(Number(x.value))).slice(-12);
 const monthNames=['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];
 const normalized=valid.map(x=>{const m=String(x.label||'').match(/^(\d{4})-(\d{2})/);return {...x,label:m?monthNames[Number(m[2])-1]+'/'+m[1].slice(-2):String(x.label||'').slice(0,9)}});
 const enough=normalized.length>=2&&normalized.some(x=>Number(x.value)>0);
 if(!enough)return '<div class="panel section"><div class="toolbar"><b>Crescimento da receita</b><span class="muted">12 meses</span></div><div class="empty">Sem dados suficientes para este período.</div></div>';
 const w=720,h=230,pad={l:12,r:12,t:18,b:32},max=Math.max(...normalized.map(x=>Number(x.value)),1);
 const pts=normalized.map((x,i)=>({x:pad.l+i*(w-pad.l-pad.r)/Math.max(1,normalized.length-1),y:pad.t+(h-pad.t-pad.b)*(1-Number(x.value)/max),...x}));
 const path=pts.map((p,i)=>(i?'L':'M')+p.x.toFixed(1)+' '+p.y.toFixed(1)).join(' ');
 const grid=[0,1,2,3].map(i=>{const y=pad.t+i*(h-pad.t-pad.b)/3;return '<line x1="'+pad.l+'" y1="'+y.toFixed(1)+'" x2="'+(w-pad.r)+'" y2="'+y.toFixed(1)+'" class="nexora-chart-grid"/>'}).join('');
 const dots=pts.map(p=>'<circle cx="'+p.x.toFixed(1)+'" cy="'+p.y.toFixed(1)+'" r="3.5" class="nexora-chart-dot"><title>'+esc(p.label)+': '+esc(money(p.value))+'</title></circle>').join('');
 const labels=pts.map((p,i)=>'<text x="'+p.x.toFixed(1)+'" y="'+(h-7)+'" text-anchor="'+(i===0?'start':i===pts.length-1?'end':'middle')+'" class="nexora-chart-label">'+esc(p.label)+'</text>').join('');
 return '<div class="panel section"><div class="toolbar"><b>Crescimento da receita</b><span class="muted">12 meses · valores registrados</span></div><div class="nexora-line-chart"><svg viewBox="0 0 '+w+' '+h+'" role="img" aria-label="Receita mensal nos últimos doze meses">'+grid+'<path d="'+path+'" class="nexora-chart-line"/>'+dots+labels+'</svg></div></div>';
}

function companyMixHtml(rows){
 const counts={LOJA:0,RESTAURANTE:0};
 (rows||[]).filter(x=>isActiveStatus(x.status)).forEach(x=>{const s=String(x.segment||'').toUpperCase();if(Object.prototype.hasOwnProperty.call(counts,s))counts[s]++});
 const total=counts.LOJA+counts.RESTAURANTE;
 if(!total)return '<div class="panel"><div class="toolbar"><b>Empresas ativas por segmento</b></div><div class="empty">Sem dados suficientes para este período.</div></div>';
 const loja=counts.LOJA/total*100;
 return '<div class="panel"><div class="toolbar"><b>Empresas ativas por segmento</b></div><div class="nexora-segment"><div class="nexora-donut" style="--loja:'+loja+'%"><span>'+number(total)+'</span></div><div class="nexora-legend"><div><i class="loja"></i><span>Loja</span><b>'+number(counts.LOJA)+'</b></div><div><i class="restaurante"></i><span>Restaurante</span><b>'+number(counts.RESTAURANTE)+'</b></div></div></div></div>';
}

function dashboardCompanyRows(rows){
 if(!rows.length) return '<div class="empty">Nenhuma empresa encontrada.</div>';

 const max=Math.max(...rows.map(x=>Number(x.revenue||0)),1);

 return '<div class="rank">'+rows.slice(0,8).map((x,i)=>{
   const pct=Math.max(2,(Number(x.revenue||0)/max)*100);
   return '<div class="rankRow">'+
    '<div class="rankN">#'+(i+1)+'</div>'+
    '<div class="rankName">'+
      '<b>'+esc(x.name)+'</b>'+
      '<span>'+number(x.sales_count)+' vendas · '+number(x.users)+' usuários</span>'+
      '<div class="progress"><i style="width:'+pct+'%"></i></div>'+
    '</div>'+
    '<b>'+esc(money(x.revenue))+'</b>'+
   '</div>';
 }).join('')+'</div>';
}

function activityRows(rows){
 if(!rows.length) return '<div class="empty">Nenhuma atividade recente.</div>';

 return '<div class="activity">'+rows.slice(0,10).map(x=>{
   const action=x.action||x.event_type||x.type||'Evento';
   const date=x.created_at||x.date||'';
   return '<div class="activityRow">'+
    '<div class="muted">'+esc(date)+'</div>'+
    '<div><span class="statusDot"></span>'+esc(action)+'</div>'+
    '<div class="muted">'+esc(x.user_id ?? x.company_id ?? '')+'</div>'+
   '</div>';
 }).join('')+'</div>';
}

const pages = {


 async dashboard(){
   loading();

   const [d,e,c,a] = await Promise.all([
     api('/master/enterprise/api/dashboard'),
     loadEvolution(),
     api('/master/enterprise/api/companies'),
     api('/master/enterprise/api/audit').catch(()=>({rows:[]})),
   ]);

   setPage('Central de Controle NEXORA','Visão global da plataforma');

   const f=d.finance||{};

   const companyRows=(c.rows||[]).slice().sort(
     (a,b)=>Number(b.revenue||0)-Number(a.revenue||0)
   );
   const activeCompanies=companyRows.filter(x=>isActiveStatus(x.status)).length;
   const inactiveCompanies=companyRows.length-activeCompanies;
   const companySales=companyRows.reduce((sum,x)=>sum+Number(x.sales_count||0),0);

   $('#content').innerHTML =
     cards([
       ['Receita',money(f.sales),'Vendas registradas'],
       ['Despesas',money(f.expenses),'Despesas registradas'],
       ['Resultado',money(f.result),'Receita menos despesas'],
       ['Ticket médio',money(f.ticket_average),'Baseado nas vendas'],
       ['Empresas ativas',number(activeCompanies),'Status cadastrado'],
       ['Empresas inativas',number(inactiveCompanies),'Status cadastrado'],
       ['Usuários ativos',number(d.active_users),'Usuários com active=1'],
       ['Vendas registradas',number(companySales),'Contagem por empresa']
     ])+
     chartHtml(extractEvolution(e))+
     '<div class="two section">'+
       '<div class="panel">'+
         '<div class="toolbar"><b>Empresas com maior movimentação</b><span class="muted">por receita</span></div>'+
         dashboardCompanyRows(companyRows)+
       '</div>'+
       companyMixHtml(companyRows)+
       '<div class="panel">'+
         '<div class="toolbar"><b>Estado da operação</b></div>'+
         '<div style="padding:15px">'+
           '<div class="statLine"><span>Uptime</span><b>'+esc(Math.floor((d.uptime_seconds||0)/3600)+'h '+Math.floor(((d.uptime_seconds||0)%3600)/60)+'m')+'</b></div>'+
           '<div class="statLine"><span>Usuários ativos</span><b>'+number(d.active_users)+'</b></div>'+
           '<div class="statLine"><span>Empresas</span><b>'+number(d.companies)+'</b></div>'+
           '<div class="statLine"><span>Alertas</span><b class="'+(Number(d.alerts)>0?'warn':'good')+'">'+number(d.alerts)+'</b></div>'+
           '<div class="notice section">'+esc(d.online_note||'Estado operacional baseado nos dados disponíveis no servidor.')+'</div>'+
         '</div>'+
       '</div>'+
     '</div>'+
     '<div class="panel section">'+
       '<div class="toolbar"><b>Atividade recente</b><button class="action" onclick="show(\'audit\')">Abrir auditoria</button></div>'+
       activityRows(a.rows||[])+
     '</div>';

 },

 async companies(){
   loading();

   const j=await api('/master/enterprise/api/companies');

   setPage('Empresas','Tenants e visão operacional');

   $('#content').innerHTML =
    '<div class="panel">'+
      '<div class="toolbar">'+
        '<input id="companySearch" placeholder="Pesquisar empresa..." oninput="filterTable(this.value,\'companyTable\')">'+
        '<button class="action" onclick="show(\'companies\')">Atualizar</button>'+
      '</div>'+
      '<div style="overflow:auto">'+
      '<table id="companyTable"><thead><tr>'+
        '<th>ID</th><th>Empresa</th><th>Status</th><th>Usuários</th><th>Produtos</th><th>Clientes</th><th>Vendas</th><th>Receita</th><th></th>'+
      '</tr></thead><tbody>'+
      (j.rows||[]).map(x=>
        '<tr>'+
        '<td>'+esc(x.id)+'</td>'+
        '<td><b>'+esc(x.name)+'</b></td>'+
        '<td class="'+(isActiveStatus(x.status)?'good':'bad')+'">'+(isActiveStatus(x.status)?'ATIVA':'INATIVA')+'</td>'+
        '<td>'+number(x.users)+'</td>'+
        '<td>'+number(x.products)+'</td>'+
        '<td>'+number(x.customers)+'</td>'+
        '<td>'+number(x.sales_count)+'</td>'+
        '<td>'+money(x.revenue)+'</td>'+
        '<td style="white-space:nowrap"><button class="action" onclick="openMasterCompany('+Number(x.id)+')">Administrar</button> <button class="action" onclick="companyDetails('+Number(x.id)+')">Detalhes</button></td>'+
        '</tr>'
      ).join('')+
      '</tbody></table></div></div>';
 },

 async users(){
   loading();

   const j=await api('/master/enterprise/api/users');

   setPage('Usuários','IAM global e controle de acesso');

   $('#content').innerHTML =
    '<div class="panel">'+
     '<div class="toolbar">'+
      '<input placeholder="Pesquisar usuário..." oninput="filterTable(this.value,\'userTable\')">'+
      '<button class="action" onclick="show(\'users\')">Atualizar</button>'+
     '</div>'+
     '<div style="overflow:auto"><table id="userTable"><thead><tr>'+
      '<th>ID</th><th>Nome</th><th>Usuário</th><th>Role</th><th>Empresa</th><th>Status</th>'+
     '</tr></thead><tbody>'+
     (j.rows||[]).map(x=>
      '<tr>'+
      '<td>'+esc(x.id)+'</td>'+
      '<td>'+esc(x.name)+'</td>'+
      '<td class="mono">'+esc(x.username)+'</td>'+
      '<td>'+esc(x.role)+'</td>'+
      '<td>'+esc(x.company_name||'Global')+'</td>'+
      '<td class="'+(x.active?'good':'bad')+'">'+(x.active?'ATIVO':'BLOQUEADO')+'</td>'+
      '</tr>'
     ).join('')+
     '</tbody></table></div></div>';
 },

 async operations(){
   loading();

   const j=await api('/master/enterprise/api/operations');

   setPage('Operações','Visão consolidada da operação das empresas');

   const counts=j.counts||{};

   $('#content').innerHTML =
     cards(Object.entries(counts).map(([k,v])=>[
       k.replaceAll('_',' '),
       number(v),
       'registros atuais'
     ]))+
     '<div class="panel section"><div class="toolbar"><b>Escopo operacional</b></div>'+
     '<div style="padding:15px" class="notice">'+
     'Produtos, estoque, vendas, compras, clientes e caixa são consolidados a partir das tabelas atuais do sistema.'+
     '</div></div>';
 },

 async finance(){
   loading();

   const j=await api('/master/enterprise/api/finance');

   setPage('Financeiro Global','Valores calculados diretamente das tabelas existentes');

   $('#content').innerHTML =
     cards([
       ['Vendas',money(j.sales),'Receita registrada'],
       ['Despesas',money(j.expenses),'Despesas registradas'],
       ['Compras',money(j.purchases),'Compras registradas'],
       ['Resultado',money(j.result),'Vendas menos despesas'],
       ['Ticket médio',money(j.ticket_average),'Média das vendas']
     ])+
     '<div class="panel section"><div class="toolbar"><b>Composição financeira</b></div>'+
     '<div style="padding:15px">'+
       '<div class="statLine"><span>Receita bruta</span><b>'+money(j.sales)+'</b></div>'+
       '<div class="statLine"><span>Despesas</span><b>'+money(j.expenses)+'</b></div>'+
       '<div class="statLine"><span>Compras</span><b>'+money(j.purchases)+'</b></div>'+
       '<div class="statLine"><span>Resultado calculado</span><b>'+money(j.result)+'</b></div>'+
     '</div></div>';
 },

 async billing(){
   loading();

   const [e,p] = await Promise.all([
     api('/master/enterprise/api/billing').catch(()=>({})),
     api('/master/plus/api/billing')
   ]);

   setPage('SaaS / Billing','Planos, mensalidades e cobrança por empresa');

   const rows = p.companies || [];
   const hist = p.history || [];
   const names = {};
   rows.forEach(x=>{ names[x.company_id] = x.company_name; });
   const st = v => String(v||'').toLowerCase();
   const overdue = rows.filter(x=>st(x.billing_status)==='overdue').length;
   const paying  = rows.filter(x=>Number(x.monthly_price||0)>0).length;
   const cls = v => (['paid','active'].includes(st(v)) ? 'good' : (['overdue','cancelled'].includes(st(v)) ? 'bad' : 'warn'));
   const lbl = {active:'ATIVA',paid:'PAGA',pending:'PENDENTE',overdue:'ATRASADA',cancelled:'CANCELADA'};

   $('#content').innerHTML =
     cards([
       ['MRR',money(p.mrr),'Soma das mensalidades cadastradas'],
       ['Empresas com mensalidade',number(paying),'mensalidade maior que zero'],
       ['Em atraso',number(overdue),'status de cobrança atrasada'],
       ['Empresas',number(rows.length),'com configuração SaaS']
     ])+
     '<div class="panel section">'+
       '<div class="toolbar"><b>Plano e cobrança por empresa</b><span class="muted">editar em Administrar</span></div>'+
       '<div style="overflow:auto"><table id="billingTable"><thead><tr>'+
         '<th>ID</th><th>Empresa</th><th>Plano</th><th>Mensalidade</th><th>Cobrança</th><th>Vencimento</th><th></th>'+
       '</tr></thead><tbody>'+
       rows.map(x=>
         '<tr>'+
         '<td>'+esc(x.company_id)+'</td>'+
         '<td><b>'+esc(x.company_name)+'</b></td>'+
         '<td>'+esc(x.plan||'—')+'</td>'+
         '<td>'+money(x.monthly_price)+'</td>'+
         '<td class="'+cls(x.billing_status)+'">'+esc(lbl[st(x.billing_status)]||String(x.billing_status||'—').toUpperCase())+'</td>'+
         '<td>'+esc(x.billing_due_date||'—')+'</td>'+
         '<td><button class="action" onclick="openMasterCompany('+Number(x.company_id)+')">Administrar</button></td>'+
         '</tr>'
       ).join('')+
       '</tbody></table></div></div>'+
     '<div class="panel section">'+
       '<div class="toolbar"><b>Histórico de cobranças</b><span class="muted">'+number(hist.length)+' registros</span></div>'+
       (hist.length
         ? '<div style="overflow:auto"><table><thead><tr><th>ID</th><th>Empresa</th><th>Valor</th><th>Status</th><th>Referência</th><th>Registrado em</th></tr></thead><tbody>'+
           hist.slice(0,50).map(h=>
             '<tr><td>'+esc(h.id)+'</td><td>'+esc(names[h.company_id]||('#'+h.company_id))+'</td><td>'+money(h.amount)+'</td>'+
             '<td class="'+cls(h.status)+'">'+esc(lbl[st(h.status)]||String(h.status||'').toUpperCase())+'</td>'+
             '<td>'+esc(h.reference||'—')+'</td><td>'+esc(h.created_at||'—')+'</td></tr>'
           ).join('')+'</tbody></table></div>'
         : '<div class="empty" style="padding:15px">Nenhuma cobrança registrada.</div>')+
     '</div>'+
     '<div class="notice section">Sem gateway financeiro conectado: os valores refletem o que foi cadastrado no MASTER.</div>';
 },

 async analytics(){
   loading();

   const j=await api('/master/enterprise/api/analytics');

   setPage('Analytics / BI','Métricas globais calculadas sem inventar dados');

   $('#content').innerHTML =
     cards([
       ['Empresas novas (30d)',number(j.new_companies_30d),'Últimos 30 dias'],
       ['Empresas inativas',number(j.inactive_companies),'Estado atual'],
       ['Empresas ativas',number(j.active_companies),'Estado atual'],
       ['Receita global',money(j.revenue),'Base consolidada']
     ]);
 },

 async security(){
   loading();

   const j=await api('/master/enterprise/api/security');

   setPage('Security Center','Sessões, eventos e postura de segurança');

   $('#content').innerHTML =
    cards([
      ['Sessões',number(j.sessions),'Registros de sessão'],
      ['Usuários ativos',number(j.active_users),'Usuários habilitados'],
      ['Eventos MASTER',number(j.events),'Eventos administrativos'],
      ['Usuários MASTER',number(j.master_users),'Contas com role master']
    ])+
    '<div class="panel section"><div class="toolbar"><b>Eventos recentes</b></div>'+
    '<div style="overflow:auto"><table><thead><tr><th>Evento</th><th>Resultado</th><th>Empresa</th><th>Data</th></tr></thead><tbody>'+
    (j.recent||[]).map(x=>
      '<tr><td>'+esc(x.event_type)+'</td><td>'+esc(x.result)+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.created_at)+'</td></tr>'
    ).join('')+
    '</tbody></table></div></div>';
 },

 async audit(){
   loading();

   const [a,m] = await Promise.all([
     api('/master/enterprise/api/audit'),
     api('/master/plus/api/audit').catch(()=>({audit:[]}))
   ]);

   setPage('Audit Center','Rastro administrativo da plataforma');

   const ops = m.audit || [];

   $('#content').innerHTML =
    '<div class="panel">'+
    '<div class="toolbar"><b>Operações MASTER</b>'+
      '<input placeholder="Pesquisar operações..." oninput="filterTable(this.value,\'opsAuditTable\')">'+
      '<button class="action" onclick="show(\'audit\')">Atualizar</button>'+
    '</div>'+
    '<div style="overflow:auto"><table id="opsAuditTable"><thead><tr>'+
      '<th>Ação</th><th>Empresa</th><th>Usuário alvo</th><th>Detalhes</th><th>IP</th><th>Data</th>'+
    '</tr></thead><tbody>'+
    (ops.length ? ops.map(x=>
      '<tr><td class="mono">'+esc(x.action)+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.target_user_id??'—')+'</td><td>'+esc(x.details||'')+'</td><td class="mono">'+esc(x.ip||'—')+'</td><td>'+esc(x.created_at)+'</td></tr>'
    ).join('') : '<tr><td colspan="6" class="muted">Nenhuma operação MASTER registrada.</td></tr>')+
    '</tbody></table></div></div>'+
    '<div class="panel section">'+
    '<div class="toolbar"><b>Auditoria da plataforma</b>'+
      '<input placeholder="Pesquisar auditoria..." oninput="filterTable(this.value,\'auditTable\')">'+
    '</div>'+
    '<div style="overflow:auto"><table id="auditTable"><thead><tr>'+
      '<th>Ação</th><th>Usuário</th><th>Empresa</th><th>Data</th>'+
    '</tr></thead><tbody>'+
    (a.rows||[]).map(x=>
      '<tr><td>'+esc(x.action||x.event_type)+'</td><td>'+esc(x.user_id??'—')+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.created_at)+'</td></tr>'
    ).join('')+
    '</tbody></table></div></div>';
 },

 async alerts(){
   loading();

   const j=await api('/master/enterprise/api/alerts');

   setPage('Alert Center','Problemas detectáveis no estado atual');

   $('#content').innerHTML =
    '<div class="panel">'+
    '<div class="toolbar"><b>Alertas detectados</b><button class="action" onclick="show(\'alerts\')">Atualizar</button></div>'+
    '<div style="overflow:auto"><table><thead><tr><th>Severidade</th><th>Tipo</th><th>Mensagem</th></tr></thead><tbody>'+
    (j.alerts||[]).map(x=>
      '<tr><td class="'+(x.severity==='high'?'bad':x.severity==='medium'?'warn':'good')+'">'+esc(x.severity)+'</td><td>'+esc(x.type)+'</td><td>'+esc(x.message)+'</td></tr>'
    ).join('')+
    '</tbody></table></div></div>';
 },

 async diagnostics(){
   loading();

   const j=await api('/master/enterprise/api/diagnostics');

   setPage('Diagnostics Center','Integridade e isolamento multiempresa');

   $('#content').innerHTML =
    '<div class="panel">'+
    '<div class="toolbar"><b>Testes do sistema</b><button class="action" onclick="show(\'diagnostics\')">Executar novamente</button></div>'+
    '<div style="overflow:auto"><table><thead><tr><th>Teste</th><th>Resultado</th></tr></thead><tbody>'+
    (j.checks||[]).map(x=>
      '<tr><td>'+esc(x.check)+'</td><td class="'+(String(x.result)==='ok'?'good':String(x.result)==='missing'?'bad':'')+'">'+esc(x.result)+'</td></tr>'
    ).join('')+
    '</tbody></table></div></div>';
 },

 async backups(){
   loading();

   const j=await api('/master/enterprise/api/backups');

   setPage('Backup & Recovery','Backups locais reais usando SQLite backup API');

   $('#content').innerHTML =
    '<div class="panel">'+
     '<div class="toolbar">'+
       '<b>Backups locais</b>'+
       '<button class="action" onclick="makeBackup()">Criar backup agora</button>'+
       '<button class="action" onclick="show(\'backups\')">Atualizar</button>'+
     '</div>'+
     '<div style="overflow:auto"><table><thead><tr><th>Arquivo</th><th>Tamanho</th><th>Data</th></tr></thead><tbody>'+
     (j.rows||[]).map(x=>
       '<tr><td class="mono">'+esc(x.name)+'</td><td>'+esc(x.size)+'</td><td>'+esc(x.modified)+'</td></tr>'
     ).join('')+
     '</tbody></table></div></div>';
 },

 async system(){
   loading();

   const j=await api('/master/enterprise/api/system');

   setPage('Sistema','Infraestrutura da instância');

   $('#content').innerHTML =
     '<div class="grid">'+
     Object.entries(j).map(([k,v])=>
       '<div class="card"><div class="k">'+esc(k.replaceAll('_',' '))+'</div><div class="v" style="font-size:18px">'+esc(String(v))+'</div></div>'
     ).join('')+
     '</div>';
 },

 async data(){
   loading();

   const j=await api('/master/enterprise/api/data');

   setPage('Data Center','Estrutura real do SQLite');

   $('#content').innerHTML =
    '<div class="panel">'+
     '<div class="toolbar"><b>Estrutura do banco</b></div>'+
     '<div style="overflow:auto"><table><thead><tr><th>Tabela</th><th>Registros</th><th>Colunas</th><th>Índices</th></tr></thead><tbody>'+
     (j.tables||[]).map(x=>
       '<tr><td class="mono">'+esc(x.name)+'</td><td>'+number(x.rows)+'</td><td>'+number(x.columns)+'</td><td>'+number(x.indexes)+'</td></tr>'
     ).join('')+
     '</tbody></table></div></div>';
 },

 async flags(){
   loading();

   const [f,c] = await Promise.all([
     api('/master/enterprise/api/flags'),
     api('/master/enterprise/api/companies')
   ]);

   setPage('Feature Flags','Controle global de módulos e liberação por empresa');

   const rows  = f.rows || [];
   const comps = (c.rows || []).slice(0,30);
   const per = await Promise.all(comps.map(x=>
     api('/master/plus/api/flags/'+Number(x.id))
       .then(r=>({id:x.id,name:x.name,map:Object.fromEntries((r.flags||[]).map(z=>[z.feature,z.enabled]))}))
       .catch(()=>({id:x.id,name:x.name,map:{}}))
   ));
   const feats = [...new Set([...rows.map(x=>x.key), ...per.flatMap(p=>Object.keys(p.map))])].filter(Boolean);

   $('#content').innerHTML =
     '<div class="panel">'+
       '<div class="toolbar"><b>Catálogo global de módulos</b><span class="muted">'+number(rows.length)+' módulos</span></div>'+
       '<div style="overflow:auto"><table><thead><tr><th>Chave</th><th>Módulo</th><th>Status</th><th>Descrição</th></tr></thead><tbody>'+
       (rows.length ? rows.map(x=>
         '<tr><td class="mono">'+esc(x.key)+'</td><td>'+esc(x.label)+'</td><td class="'+(x.enabled?'good':'warn')+'">'+(x.enabled?'ON':'OFF')+'</td><td>'+esc(x.description||'')+'</td></tr>'
       ).join('') : '<tr><td colspan="4" class="muted">Nenhuma flag cadastrada.</td></tr>')+
       '</tbody></table></div></div>'+
     '<div class="panel section">'+
       '<div class="toolbar"><b>Liberação por empresa</b><span class="muted">alterar em Gerenciar</span></div>'+
       '<div style="overflow:auto"><table id="flagMatrix"><thead><tr><th>Empresa</th>'+
       feats.map(k=>'<th class="mono">'+esc(k)+'</th>').join('')+
       '</tr></thead><tbody>'+
       (per.length ? per.map(p=>
         '<tr><td><b>'+esc(p.name)+'</b> <span class="muted">#'+esc(p.id)+'</span></td>'+
         feats.map(k=> (k in p.map)
           ? '<td class="'+(p.map[k]?'good':'warn')+'">'+(p.map[k]?'ON':'OFF')+'</td>'
           : '<td class="muted">—</td>').join('')+'</tr>'
       ).join('') : '<tr><td class="muted">Nenhuma empresa.</td></tr>')+
       '</tbody></table></div></div>';
 },

 async api(){
   loading();

   const [k,w,i] = await Promise.all([
     api('/master/plus/api/integrations/keys'),
     api('/master/plus/api/integrations/webhooks'),
     api('/master/plus/api/integrations')
   ]);

   setPage('API / Integrações','Chaves, webhooks e integrações cadastradas');

   const keys = k.keys || [], hooks = w.webhooks || [], ints = i.integrations || [];
   const onoff = v => '<td class="'+(v?'good':'warn')+'">'+(v?'ATIVA':'INATIVA')+'</td>';
   const empty = (n,t) => '<tr><td colspan="'+n+'" class="muted">'+t+'</td></tr>';

   $('#content').innerHTML =
     cards([
       ['API keys',number(keys.length),number(keys.filter(x=>x.active).length)+' ativas'],
       ['Webhooks',number(hooks.length),number(hooks.filter(x=>x.active).length)+' ativos'],
       ['Integrações',number(ints.length),'cadastradas']
     ])+
     '<div class="panel section"><div class="toolbar"><b>Chaves de API</b></div><div style="overflow:auto"><table><thead><tr>'+
       '<th>ID</th><th>Empresa</th><th>Nome</th><th>Prefixo</th><th>Status</th><th>Último uso</th><th>Criada em</th></tr></thead><tbody>'+
       (keys.length ? keys.map(x=>'<tr><td>'+esc(x.id)+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.name)+'</td><td class="mono">'+esc(x.key_prefix||'—')+'</td>'+onoff(x.active)+'<td>'+esc(x.last_used_at||'—')+'</td><td>'+esc(x.created_at||'—')+'</td></tr>').join('') : empty(7,'Nenhuma chave criada.'))+
     '</tbody></table></div></div>'+
     '<div class="panel section"><div class="toolbar"><b>Webhooks</b></div><div style="overflow:auto"><table><thead><tr>'+
       '<th>ID</th><th>Empresa</th><th>Nome</th><th>URL</th><th>Evento</th><th>Status</th><th>Última resposta</th></tr></thead><tbody>'+
       (hooks.length ? hooks.map(x=>'<tr><td>'+esc(x.id)+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.name||'—')+'</td><td class="mono">'+esc(x.url)+'</td><td class="mono">'+esc(x.event||'*')+'</td>'+onoff(x.active)+'<td>'+esc(x.last_status||'—')+'</td></tr>').join('') : empty(7,'Nenhum webhook cadastrado.'))+
     '</tbody></table></div></div>'+
     '<div class="panel section"><div class="toolbar"><b>Integrações</b></div><div style="overflow:auto"><table><thead><tr>'+
       '<th>ID</th><th>Empresa</th><th>Provedor</th><th>Categoria</th><th>Status</th><th>Atualizada em</th></tr></thead><tbody>'+
       (ints.length ? ints.map(x=>'<tr><td>'+esc(x.id)+'</td><td>'+esc(x.company_id??'—')+'</td><td>'+esc(x.provider||'—')+'</td><td>'+esc(x.category||'—')+'</td><td>'+esc(x.status||'—')+'</td><td>'+esc(x.updated_at||x.created_at||'—')+'</td></tr>').join('') : empty(6,'Nenhuma integração configurada. WhatsApp, NF-e e gateways dependem de conector real.'))+
     '</tbody></table></div></div>';
 },

 async maintenance(){
   loading();

   const j=await api('/master/api/maintenance');

   setPage('Manutenção','Controle da janela de manutenção global');

   $('#content').innerHTML =
    '<div class="card">'+
      '<div class="k">Status atual</div>'+
      '<div class="v">'+(j.enabled?'<span class="warn">ATIVA</span>':'<span class="good">INATIVA</span>')+'</div>'+
      '<p class="muted">'+esc(j.message||'Nenhuma mensagem de manutenção.')+'</p>'+
      '<button class="action '+(j.enabled?'danger':'')+'" onclick="toggleMaintenance('+(!j.enabled)+')">'+
        (j.enabled?'Desativar manutenção':'Ativar manutenção')+
      '</button>'+
    '</div>';
 },

 async search(){
   setPage('Busca Global','Pesquisar informações administrativas da plataforma');

   $('#content').innerHTML =
    '<div class="panel">'+
      '<div class="toolbar">'+
       '<input id="globalSearch" style="flex:1;min-width:240px" placeholder="Empresa, usuário, cliente, produto, venda...">'+
       '<button class="action" onclick="globalSearch()">Pesquisar</button>'+
      '</div>'+
      '<div id="searchResults" class="empty">Digite um termo para pesquisar.</div>'+
    '</div>';
 }

};


async function globalSearch(){
 const input=$('#globalSearch');
 const term=(input?.value||'').trim();

 if(!term){
   $('#searchResults').innerHTML='<div class="empty">Digite um termo.</div>';
   return;
 }

 $('#searchResults').innerHTML='<div class="loading">Pesquisando...</div>';

 try{
   const j=await api('/master/api/search?q='+encodeURIComponent(term));

   const rows=j.items||j.rows||j.results||[];

   if(!rows.length){
     $('#searchResults').innerHTML='<div class="empty">Nenhum resultado encontrado.</div>';
     return;
   }

   $('#searchResults').innerHTML =
    '<div style="overflow:auto"><table><thead><tr>'+
    '<th>Tipo</th><th>ID</th><th>Nome</th><th>Empresa</th><th>Informação</th>'+
    '</tr></thead><tbody>'+
    rows.map(x=>
      '<tr>'+
      '<td>'+esc(x.type||x.entity||'')+'</td>'+
      '<td>'+esc(x.id??'')+'</td>'+
      '<td>'+esc(x.name||x.username||x.title||'')+'</td>'+
      '<td>'+esc(x.company_name||x.company_id||'')+'</td>'+
      '<td>'+esc(x.description||x.detail||x.info||x.username||x.role||'')+'</td>'+
      '</tr>'
    ).join('')+
    '</tbody></table></div>';

 }catch(e){
   $('#searchResults').innerHTML='<div class="error">'+esc(e.message)+'</div>';
 }
}

const globalSearchInput = document.getElementById('globalSearch');
if(globalSearchInput){
 globalSearchInput.addEventListener('keydown', function(event){
  if(event.key === 'Enter'){
   event.preventDefault();
   globalSearch();
  }
 });
}

async function companyDetails(id){
 try{
   const j=await api('/master/enterprise/api/companies');

   const x=(j.rows||[]).find(r=>Number(r.id)===Number(id));

   if(!x){
     openModal('Empresa','Empresa não encontrada.');
     return;
   }

   openModal(
     x.name||('Empresa '+id),
     '<div class="grid">'+
       '<div class="card"><div class="k">ID</div><div class="v">'+esc(x.id)+'</div></div>'+
       '<div class="card"><div class="k">Status</div><div class="v" style="font-size:18px">'+(isActiveStatus(x.status)?'ATIVA':'INATIVA')+'</div></div>'+
       '<div class="card"><div class="k">Usuários</div><div class="v">'+number(x.users)+'</div></div>'+
       '<div class="card"><div class="k">Produtos</div><div class="v">'+number(x.products)+'</div></div>'+
       '<div class="card"><div class="k">Clientes</div><div class="v">'+number(x.customers)+'</div></div>'+
       '<div class="card"><div class="k">Vendas</div><div class="v">'+number(x.sales_count)+'</div></div>'+
       '<div class="card"><div class="k">Receita</div><div class="v">'+money(x.revenue)+'</div></div>'+
     '</div>'
   );
 }catch(e){
   openModal('Erro',esc(e.message));
 }
}

async function makeBackup(){
 try{
   const j=await api('/master/enterprise/api/backup',{method:'POST'});
   alert(j.message||j.path||'Backup criado.');
   show('backups');
 }catch(e){
   alert('Erro ao criar backup: '+e.message);
 }
}

async function toggleMaintenance(enabled){
 const message = enabled
   ? 'Manutenção administrativa do Master.'
   : '';

 try{
   const j=await api('/master/api/maintenance',{
     method:'POST',
     body:JSON.stringify({enabled,message})
   });

   alert(j.message||'Operação concluída.');
   show('maintenance');
 }catch(e){
   alert('Erro: '+e.message);
 }
}

function filterTable(value,id){
 const q=String(value||'').toLowerCase();
 const table=document.getElementById(id);

 if(!table) return;

 table.querySelectorAll('tbody tr').forEach(tr=>{
   tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none';
 });
}

const MANAGE = {
 companies:['companies','Criar empresa, definir plano e limites'],
 users:['users','Criar administrador, alterar papel, redefinir senha, encerrar sessões'],
 security:['security','Sessões ativas e encerramento global'],
 billing:['billing','Planos e cobrança'],
 flags:['flags','Gerenciar feature flags'],
 system:['system','Configurações do sistema'],
 api:['integrations','Integrações e chaves de API']
};

function isActiveStatus(v){
 const s=String(v ?? '').toLowerCase().trim();
 return !(s==='0'||s==='false'||s==='none'||s.includes('inativ')||s.includes('suspens')||s.includes('bloque'));
}

function openExpansionTab(tab){
 if(!window.MasterControlExpansion){ alert('Módulo de ações avançadas não carregado.'); return; }
 window.MasterControlExpansion.open(tab);
}

function decoratePage(page){
 const m=MANAGE[page];
 if(!m || !window.MasterControlExpansion) return;
 const c=$('#content');
 if(!c || c.querySelector('.manage-bar')) return;
 const bar=document.createElement('div');
 bar.className='manage-bar';
 bar.innerHTML='<span>'+esc(m[1])+'</span><button class="action" onclick="openExpansionTab(\''+m[0]+'\')">Gerenciar</button>';
 c.insertBefore(bar,c.firstChild);
}

function goStore(){
 window.open('/','_blank','noopener');
}

async function logoutMaster(){
 if(!confirm('Encerrar a sessão MASTER?')) return;
 try{ await api('/api/logout',{method:'POST'}); }catch(e){}
 window.location.href='/';
}

$$('.nav button[data-page]').forEach(button=>{
 button.addEventListener('click',()=>{
   show(button.dataset.page);
 });
});

document.addEventListener('keydown',e=>{
 if(e.key==='Escape') closeModal();
});

$('#modalBack').addEventListener('click',e=>{
 if(e.target.id==='modalBack') closeModal();
});

window.addEventListener('online',()=>{if(!masterRealtimeSocket)masterRealtimeConnect()});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!masterRealtimeSocket)masterRealtimeConnect()});
document.addEventListener('click',e=>{
 if(e.target.closest('.mcc-close,#master-exp-close'))setTimeout(masterRealtimeModalClosed,80);
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')setTimeout(masterRealtimeModalClosed,80)});
masterRealtimeConnect();
masterFallbackTimer=setInterval(()=>{
 if((!window.WebSocket||!masterRealtimeSocket||masterRealtimeSocket.readyState!==WebSocket.OPEN)&&!document.hidden)masterRealtimeReceive({type:'admin_changed',entity:'administration'});
},45000);

show('dashboard');


</script>
<script id="master-company-control-v1">
// MASTER COMPANY CONTROL JS V1

let mccCompanyId = null;
let mccCompanyData = null;

function mccLog(message){
    const el = document.getElementById("mcc-log");
    if(!el) return;

    const now = new Date().toLocaleTimeString();
    el.textContent = `[${now}] ${message}\n` + el.textContent;
}

function mccSet(id,value){
    const el = document.getElementById(id);
    if(el) el.value = value ?? "";
}

function mccText(id,value){
    const el = document.getElementById(id);
    if(el) el.textContent = value ?? "—";
}

function mccMoney(v){
    const n = Number(v || 0);
    try{
        return n.toLocaleString("pt-BR",{
            style:"currency",
            currency:"BRL"
        });
    }catch(e){
        return "R$ " + n.toFixed(2);
    }
}

async function mccJson(url,options={}){
    const response = await fetch(url,{
        credentials:"same-origin",
        ...options,
        headers:{
            ...(options.headers||{})
        }
    });

    let data = null;

    try{
        data = await response.json();
    }catch(e){
        data = {};
    }

    if(!response.ok || data.ok === false){
        throw new Error(
            data.error ||
            data.message ||
            `HTTP ${response.status}`
        );
    }

    return data;
}

window.openMasterCompany = async function(id){
    mccCompanyId = Number(id);

    const overlay = document.getElementById("mcc-overlay");
    overlay.classList.add("open");

    mccText("mcc-title","Carregando empresa...");
    mccText("mcc-subtitle","MASTER CONTROL");
    mccLog(`Abrindo empresa #${mccCompanyId}`);

    try{
        await mccLoadCompany();
        await mccLoadSupport();
        await mccSnapshot();

        mccText(
            "mcc-title",
            mccCompanyData?.company?.name ||
            `Empresa #${mccCompanyId}`
        );

        mccText(
            "mcc-subtitle",
            `MASTER CONTROL • Empresa #${mccCompanyId}`
        );

    }catch(e){
        mccLog("ERRO: " + e.message);
        alert("Não foi possível carregar a empresa.\n\n" + e.message);
    }
};

window.closeMasterCompany = function(){
    document.getElementById("mcc-overlay")?.classList.remove("open");
    mccCompanyId = null;
    mccCompanyData = null;
    masterRealtimeModalClosed();
};

async function mccLoadCompany(){
    if(!mccCompanyId) return;

    const raw = await mccJson(
        `/master/plus/api/company/${mccCompanyId}`
    );

    const d = raw.data || raw;

    mccCompanyData = d;

    const c = d.company || {};
    const u = d.usage || {};
    const cfg = d.config || {};

    mccText("mcc-users",u.users ?? 0);
    mccText("mcc-products",u.products ?? 0);
    mccText("mcc-customers",u.customers ?? 0);
    mccText("mcc-sales",u.sales ?? 0);

    mccSet("mcc-plan",cfg.plan || "standard");
    mccSet("mcc-price",cfg.monthly_price ?? 0);
    mccSet("mcc-billing-status",cfg.billing_status || "active");

    mccSet("mcc-max-users",cfg.max_users ?? 0);
    mccSet("mcc-max-products",cfg.max_products ?? 0);
    mccSet("mcc-max-customers",cfg.max_customers ?? 0);
    mccSet("mcc-max-sales",cfg.max_sales_month ?? 0);
    mccSet("mcc-max-storage",cfg.max_storage_mb ?? 0);

    mccLog(
        `Empresa carregada: ${c.name || mccCompanyId} • ` +
        `Status: ${c.active ? "ATIVA" : "INATIVA"}`
    );
}

async function mccEditName(){
    if(!mccCompanyId) return;

    const current =
        mccCompanyData?.company?.name || "";

    const name = prompt(
        "Novo nome da empresa:",
        current
    );

    if(!name || name.trim() === current) return;

    try{
        await mccJson(
            `/master/api/companies/${mccCompanyId}`,
            {
                method:"PUT",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify({
                    name:name.trim()
                })
            }
        );

        mccLog("Nome da empresa atualizado.");
        await mccLoadCompany();

        if(typeof loadCompanies === "function"){
            loadCompanies();
        }

    }catch(e){
        alert(e.message);
    }
}

async function mccActivate(){
    if(!mccCompanyId) return;

    if(!confirm("Ativar esta empresa?")) return;

    try{
        await mccJson(
            `/master/plus/api/company/${mccCompanyId}/activate`,
            {method:"POST"}
        );

        mccLog("Empresa ativada.");
        await mccLoadCompany();

        if(typeof loadCompanies === "function"){
            loadCompanies();
        }

    }catch(e){
        alert(e.message);
    }
}

async function mccSuspend(){
    if(!mccCompanyId) return;

    if(!confirm(
        "Suspender esta empresa?\n\n" +
        "Os usuários da empresa poderão perder acesso."
    )) return;

    try{
        await mccJson(
            `/master/plus/api/company/${mccCompanyId}/suspend`,
            {method:"POST"}
        );

        mccLog("Empresa suspensa.");
        await mccLoadCompany();

        if(typeof loadCompanies === "function"){
            loadCompanies();
        }

    }catch(e){
        alert(e.message);
    }
}

async function mccCreateAdmin(){
    if(!mccCompanyId) return;

    const name = prompt("Nome do administrador:");
    if(!name) return;

    const username = prompt("Username:");
    if(!username) return;

    const password = prompt(
        "Senha inicial (mínimo 6 caracteres):"
    );

    if(!password) return;

    try{
        await mccJson(
            "/master/api/users/create-admin",
            {
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify({
                    company_id:mccCompanyId,
                    name:name.trim(),
                    username:username.trim(),
                    password
                })
            }
        );

        mccLog(
            `Administrador ${username} criado.`
        );

        alert("Administrador criado com sucesso.");

        if(typeof loadUsers === "function"){
            loadUsers();
        }

    }catch(e){
        alert(e.message);
    }
}

function mccOpenUsers(){
    document.getElementById("mcc-overlay").classList.remove("open");
    show("users");
}

async function mccRevokeSessions(){
    if(!mccCompanyId) return;

    if(!confirm(
        "Encerrar TODAS as sessões desta empresa?"
    )) return;

    try{
        const d = await mccJson(
            `/master/api/companies/${mccCompanyId}/sessions/revoke`,
            {method:"POST"}
        );

        mccLog(
            `${d.deleted || 0} sessão(ões) encerrada(s).`
        );

        alert(
            `${d.deleted || 0} sessão(ões) encerrada(s).`
        );

    }catch(e){
        alert(e.message);
    }
}

async function mccSavePlan(){
    if(!mccCompanyId) return;

    const plan =
        document.getElementById("mcc-plan")?.value ||
        "standard";

    const monthly_price =
        Number(
            document.getElementById("mcc-price")?.value || 0
        );

    const billing_status =
        document.getElementById("mcc-billing-status")?.value ||
        "active";

    try{
        await mccJson(
            `/master/plus/api/company/${mccCompanyId}/plan`,
            {
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify({
                    plan,
                    monthly_price,
                    billing_status
                })
            }
        );

        mccLog(
            `Plano salvo: ${plan} • ${mccMoney(monthly_price)} • ${billing_status}`
        );

        alert("Plano e cobrança atualizados.");

        await mccLoadCompany();

    }catch(e){
        alert(e.message);
    }
}

async function mccRegisterCharge(){
    if(!mccCompanyId) return;

    const amountText = prompt(
        "Valor da cobrança (R$):",
        document.getElementById("mcc-price")?.value || "0"
    );

    if(amountText === null) return;

    const amount = Number(
        String(amountText)
            .replace(/\./g,"")
            .replace(",",".")
    );

    if(!Number.isFinite(amount) || amount <= 0){
        alert("Valor inválido.");
        return;
    }

    const status = prompt(
        "Status da cobrança:",
        "pending"
    );

    if(status === null) return;

    try{
        await mccJson(
            "/master/plus/api/billing/history",
            {
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify({
                    company_id:mccCompanyId,
                    amount,
                    status:String(status).trim() || "pending"
                })
            }
        );

        mccLog(
            `Cobrança registrada: ${mccMoney(amount)} • ${status}`
        );

        alert("Cobrança registrada.");

    }catch(e){
        alert(e.message);
    }
}

async function mccSaveLimits(){
    if(!mccCompanyId) return;

    const body = {
        max_users:Number(
            document.getElementById("mcc-max-users")?.value || 0
        ),
        max_products:Number(
            document.getElementById("mcc-max-products")?.value || 0
        ),
        max_customers:Number(
            document.getElementById("mcc-max-customers")?.value || 0
        ),
        max_sales_month:Number(
            document.getElementById("mcc-max-sales")?.value || 0
        ),
        max_storage_mb:Number(
            document.getElementById("mcc-max-storage")?.value || 0
        )
    };

    try{
        await mccJson(
            `/master/plus/api/company/${mccCompanyId}/limits`,
            {
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify(body)
            }
        );

        mccLog("Limites da empresa atualizados.");
        alert("Limites salvos.");

        await mccLoadCompany();

    }catch(e){
        alert(e.message);
    }
}

async function mccStartSupport(){
    if(!mccCompanyId) return;

    try{
        await mccJson(
            `/master/plus/api/support/start/${mccCompanyId}`,
            {
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify({reason:"Suporte MASTER via Company Control"})
            }
        );

        mccLog("Sessão de suporte MASTER iniciada.");
        await mccLoadSupport();

    }catch(e){
        alert(e.message);
    }
}

async function mccStopSupport(){
    try{
        await mccJson(
            "/master/plus/api/support/stop",
            {method:"POST"}
        );

        mccLog("Sessão de suporte encerrada.");
        await mccLoadSupport();

    }catch(e){
        alert(e.message);
    }
}

async function mccLoadSupport(){
    const el = document.getElementById(
        "mcc-support-status"
    );

    if(!el) return;

    try{
        const d = await mccJson(
            "/master/plus/api/support/current"
        );

        if(d.active){
            el.textContent =
                "SUPORTE ATIVO" +
                (d.company_name
                    ? " • " + d.company_name
                    : "");
        }else{
            el.textContent = "SUPORTE INATIVO";
        }

    }catch(e){
        el.textContent = "Status indisponível";
    }
}

async function mccSnapshot(){
    if(!mccCompanyId) return;

    const el = document.getElementById(
        "mcc-snapshot"
    );

    if(el){
        el.textContent = "Carregando snapshot...";
    }

    try{
        const d = await mccJson(
            `/master/plus/api/support/snapshot/${mccCompanyId}`
        );

        if(el){
            el.textContent =
                JSON.stringify(d,null,2);
        }

        mccLog("Snapshot atualizado.");

    }catch(e){
        if(el){
            el.textContent =
                "Erro: " + e.message;
        }

        mccLog(
            "Falha no snapshot: " + e.message
        );
    }
}


</script>
<script src="/static/master_control_expansion.js"></script>
<script>
window.openMasterAdvancedActions=function(){
 if(window.MasterControlExpansion&&typeof window.MasterControlExpansion.open==="function"){window.MasterControlExpansion.open();return;}
 alert("Módulo de ações avançadas ainda não foi carregado.");
};
</script>
</body>
</html>

"""


if __name__ == "__main__":
    print("master_control.py não deve ser executado diretamente.")
