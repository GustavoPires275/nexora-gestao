from pathlib import Path
from datetime import datetime
import sqlite3
import platform

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "loja.db"


def db():
    return sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False
    )


def table_exists(conn, table):
    return bool(conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table,)
    ).fetchone())


def columns(conn, table):
    if not table_exists(conn, table):
        return []

    return [
        row[1]
        for row in conn.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
    ]


def count_rows(conn, table):
    if not table_exists(conn, table):
        return 0

    try:
        return conn.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
    except Exception:
        return 0


def sum_column(conn, table, column):
    if not table_exists(conn, table):
        return 0

    if column not in columns(conn, table):
        return 0

    try:
        return conn.execute(
            f'SELECT COALESCE(SUM("{column}"), 0) FROM "{table}"'
        ).fetchone()[0]
    except Exception:
        return 0


def require_super_master(request: Request, get_user):
    user = get_user(request)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Sessao nao autenticada"
        )

    if str(user.get("role", "")).lower() != "master":
        raise HTTPException(
            status_code=403,
            detail="Acesso exclusivo do SUPER MASTER"
        )

    return user


def register_super_master(app, get_user, audit=None, now=None):

    def guard(request):
        return require_super_master(request, get_user)

    @app.get("/master/super")
    @app.get("/master/super/")
    async def super_master_page(request: Request):
        guard(request)
        return HTMLResponse("""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SUPER MASTER</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;
    background: #070a0f;
    color: #f5f7fa;
    font-family: Arial, Helvetica, sans-serif;
}

.app {
    min-height: 100vh;
    display: flex;
}

.sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    width: 250px;
    background: #0b0f15;
    border-right: 1px solid #202832;
    padding: 22px 14px;
}

.logo {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    display: grid;
    place-items: center;
    background: linear-gradient(145deg,#f0ca70,#9b6b22);
    color: #111;
    font-weight: 900;
    font-size: 20px;
    margin-bottom: 12px;
}

.brand {
    padding: 8px 12px 25px;
}

.brand h2 {
    margin: 0;
    font-size: 16px;
}

.brand p {
    margin: 5px 0 0;
    color: #8994a3;
    font-size: 12px;
}

.nav button {
    width: 100%;
    margin: 4px 0;
    padding: 12px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: #aeb8c5;
    text-align: left;
    cursor: pointer;
    font-size: 14px;
}

.nav button:hover,
.nav button.active {
    background: #151c25;
    color: #fff;
}

.main {
    margin-left: 250px;
    width: calc(100% - 250px);
    padding: 30px;
}

.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 25px;
}

.header h1 {
    margin: 0;
    font-size: 28px;
}

.header p {
    color: #8994a3;
    margin: 6px 0 0;
}

.online {
    color: #55d99a;
    font-size: 13px;
}

.cards {
    display: grid;
    grid-template-columns: repeat(auto-fit,minmax(180px,1fr));
    gap: 14px;
}

.card {
    background: #0d1219;
    border: 1px solid #202832;
    border-radius: 16px;
    padding: 18px;
}

.label {
    color: #8994a3;
    font-size: 12px;
}

.value {
    margin-top: 9px;
    font-size: 27px;
    font-weight: 800;
}

.page {
    display: none;
}

.page.active {
    display: block;
}

.panel {
    margin-top: 20px;
    background: #0d1219;
    border: 1px solid #202832;
    border-radius: 16px;
    overflow: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 12px 14px;
    border-bottom: 1px solid #202832;
    text-align: left;
    font-size: 13px;
}

th {
    color: #8994a3;
}

.status {
    padding: 5px 9px;
    border-radius: 999px;
    background: #11271e;
    color: #5be0a0;
}

pre {
    padding: 20px;
    white-space: pre-wrap;
    color: #bac4d0;
}

@media(max-width:800px) {

    .sidebar {
        width: 70px;
    }

    .brand h2,
    .brand p,
    .nav span {
        display: none;
    }

    .main {
        margin-left: 70px;
        width: calc(100% - 70px);
        padding: 18px;
    }
}

</style>
</head>

<body>

<div class="app">

<aside class="sidebar">

<div class="brand">

<div class="logo">S</div>

<h2>SUPER MASTER</h2>

<p>Gestao Global SaaS</p>

</div>

<div class="nav">

<button class="active" onclick="showPage('dashboard',this)">
◈ <span>Dashboard</span>
</button>

<button onclick="showPage('companies',this)">
▣ <span>Empresas</span>
</button>

<button onclick="showPage('users',this)">
◉ <span>Usuarios</span>
</button>

<button onclick="showPage('audit',this)">
◇ <span>Auditoria</span>
</button>

<button onclick="showPage('platform',this)">
◫ <span>Plataforma</span>
</button>

<button onclick="showPage('health',this)">
● <span>Saude</span>
</button>

</div>

</aside>

<main class="main">

<div class="header">

<div>
<h1 id="title">Dashboard</h1>
<p>Controle global da plataforma</p>
</div>

<div class="online">
● SUPER MASTER ONLINE
</div>

</div>

<section id="dashboard" class="page active">

<div class="cards">

<div class="card">
<div class="label">Empresas</div>
<div class="value" id="companies">—</div>
</div>

<div class="card">
<div class="label">Usuarios</div>
<div class="value" id="users">—</div>
</div>

<div class="card">
<div class="label">Produtos</div>
<div class="value" id="products">—</div>
</div>

<div class="card">
<div class="label">Clientes</div>
<div class="value" id="customers">—</div>
</div>

<div class="card">
<div class="label">Vendas</div>
<div class="value" id="sales">—</div>
</div>

<div class="card">
<div class="label">Despesas</div>
<div class="value" id="expenses">—</div>
</div>

</div>

</section>

<section id="companies" class="page">

<div class="panel">

<table>

<thead>
<tr>
<th>ID</th>
<th>Empresa</th>
<th>Status</th>
</tr>
</thead>

<tbody id="companiesBody"></tbody>

</table>

</div>

</section>

<section id="users" class="page">

<div class="panel">

<table>

<thead>
<tr>
<th>ID</th>
<th>Nome</th>
<th>Usuario</th>
<th>Perfil</th>
<th>Empresa</th>
<th>Status</th>
</tr>
</thead>

<tbody id="usersBody"></tbody>

</table>

</div>

</section>

<section id="audit" class="page">

<div class="panel">

<pre id="auditBody">Carregando...</pre>

</div>

</section>

<section id="platform" class="page">

<div class="panel">

<pre id="platformBody">Carregando...</pre>

</div>

</section>

<section id="health" class="page">

<div class="cards">

<div class="card">
<div class="label">Status</div>
<div class="value" id="healthStatus">—</div>
</div>

<div class="card">
<div class="label">SQLite</div>
<div class="value" id="healthSQLite">—</div>
</div>

<div class="card">
<div class="label">Integridade</div>
<div class="value" id="healthIntegrity">—</div>
</div>

</div>

</section>

</main>

</div>

<script>

const titles = {
    dashboard: "Dashboard",
    companies: "Empresas",
    users: "Usuarios",
    audit: "Auditoria",
    platform: "Plataforma",
    health: "Saude"
};

async function api(url) {

    const response = await fetch(url);

    if (!response.ok) {
        throw new Error("HTTP " + response.status);
    }

    return response.json();
}

function showPage(page, button) {

    document
        .querySelectorAll(".page")
        .forEach(function(x) {
            x.classList.remove("active");
        });

    document
        .getElementById(page)
        .classList.add("active");

    document
        .querySelectorAll(".nav button")
        .forEach(function(x) {
            x.classList.remove("active");
        });

    button.classList.add("active");

    document.getElementById("title").textContent =
        titles[page];

    load(page);
}

async function dashboard() {

    try {

        const data =
            await api("/master/api/super-dashboard");

        document.getElementById("companies").textContent =
            data.companies ?? 0;

        document.getElementById("users").textContent =
            data.users ?? 0;

        document.getElementById("products").textContent =
            data.products ?? 0;

        document.getElementById("customers").textContent =
            data.customers ?? 0;

        document.getElementById("sales").textContent =
            data.sales ?? 0;

        document.getElementById("expenses").textContent =
            data.expenses ?? 0;

    } catch(error) {

        console.error(error);

    }
}

async function companies() {

    const data =
        await api("/master/api/super-companies");

    const body =
        document.getElementById("companiesBody");

    body.innerHTML = "";

    for (const company of data.companies || []) {

        const row =
            document.createElement("tr");

        row.innerHTML =
            "<td>" + (company.id ?? "—") + "</td>" +
            "<td>" + (company.name ?? "—") + "</td>" +
            "<td><span class='status'>" +
            (company.active ? "Ativa" : "Inativa") +
            "</span></td>";

        body.appendChild(row);
    }
}

async function users() {

    const data =
        await api("/master/api/super-users");

    const body =
        document.getElementById("usersBody");

    body.innerHTML = "";

    for (const user of data.users || []) {

        const row =
            document.createElement("tr");

        row.innerHTML =
            "<td>" + (user.id ?? "—") + "</td>" +
            "<td>" + (user.name ?? "—") + "</td>" +
            "<td>" + (user.username ?? "—") + "</td>" +
            "<td>" + (user.role ?? "—") + "</td>" +
            "<td>" + (user.company_id ?? "GLOBAL") + "</td>" +
            "<td>" + (user.active ? "Ativo" : "Inativo") + "</td>";

        body.appendChild(row);
    }
}

async function audit() {

    const data =
        await api("/master/api/super-audit");

    document.getElementById("auditBody").textContent =
        JSON.stringify(data.logs || [], null, 2);
}

async function platform() {

    const data =
        await api("/master/api/super-platform");

    document.getElementById("platformBody").textContent =
        JSON.stringify(data.tables || [], null, 2);
}

async function health() {

    const data =
        await api("/master/api/super-health");

    document.getElementById("healthStatus").textContent =
        data.status || "—";

    document.getElementById("healthSQLite").textContent =
        data.sqlite || "—";

    document.getElementById("healthIntegrity").textContent =
        data.integrity || "—";
}

function load(page) {

    if (page === "dashboard") dashboard();
    if (page === "companies") companies();
    if (page === "users") users();
    if (page === "audit") audit();
    if (page === "platform") platform();
    if (page === "health") health();
}

dashboard();

</script>

</body>
</html>
""")

    @app.get("/master/api/super-dashboard")
    async def super_dashboard(request: Request):

        user = guard(request)
        conn = db()

        try:
            return {
                "ok": True,
                "master": user.get("username"),
                "companies": count_rows(conn, "companies"),
                "users": count_rows(conn, "users"),
                "products": count_rows(conn, "products"),
                "customers": count_rows(conn, "customers"),
                "sales": count_rows(conn, "sales"),
                "expenses": count_rows(conn, "expenses"),
                "sales_total": sum_column(conn, "sales", "total"),
                "expenses_total": sum_column(conn, "expenses", "amount"),
                "time": datetime.now().isoformat(
                    timespec="seconds"
                )
            }
        finally:
            conn.close()

    @app.get("/master/api/super-companies")
    async def super_companies(request: Request):

        guard(request)
        conn = db()

        try:

            if not table_exists(conn, "companies"):
                return {
                    "ok": True,
                    "companies": []
                }

            cursor = conn.execute(
                "SELECT * FROM companies ORDER BY id DESC"
            )

            rows = cursor.fetchall()
            names = [item[0] for item in cursor.description]

            return {
                "ok": True,
                "companies": [
                    dict(zip(names, row))
                    for row in rows
                ]
            }

        finally:
            conn.close()

    @app.get("/master/api/super-users")
    async def super_users(request: Request):

        guard(request)
        conn = db()

        try:

            if not table_exists(conn, "users"):
                return {
                    "ok": True,
                    "users": []
                }

            cursor = conn.execute(
                """
                SELECT
                    id,
                    name,
                    username,
                    role,
                    active,
                    company_id,
                    created_at
                FROM users
                ORDER BY id DESC
                """
            )

            rows = cursor.fetchall()
            names = [item[0] for item in cursor.description]

            return {
                "ok": True,
                "users": [
                    dict(zip(names, row))
                    for row in rows
                ]
            }

        finally:
            conn.close()

    @app.get("/master/api/super-audit")
    async def super_audit(request: Request):

        guard(request)
        conn = db()

        try:

            if not table_exists(conn, "audit_log"):
                return {
                    "ok": True,
                    "logs": []
                }

            cursor = conn.execute(
                """
                SELECT *
                FROM audit_log
                ORDER BY rowid DESC
                LIMIT 300
                """
            )

            rows = cursor.fetchall()
            names = [item[0] for item in cursor.description]

            return {
                "ok": True,
                "logs": [
                    dict(zip(names, row))
                    for row in rows
                ]
            }

        finally:
            conn.close()

    @app.get("/master/api/super-health")
    async def super_health(request: Request):

        guard(request)
        conn = db()

        try:

            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

            return {
                "ok": True,
                "status": "online",
                "integrity": integrity,
                "sqlite": sqlite3.sqlite_version,
                "python": platform.python_version(),
                "database_size": DB_PATH.stat().st_size
            }

        finally:
            conn.close()

    @app.get("/master/api/super-platform")
    async def super_platform(request: Request):

        guard(request)
        conn = db()

        try:

            cursor = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                AND name LIKE 'master_%'
                ORDER BY name
                """
            )

            rows = cursor.fetchall()

            result = []

            for row in rows:

                table = row[0]

                result.append({
                    "name": table,
                    "records": count_rows(
                        conn,
                        table
                    ),
                    "columns": columns(
                        conn,
                        table
                    )
                })

            return {
                "ok": True,
                "tables": result
            }

        finally:
            conn.close()
