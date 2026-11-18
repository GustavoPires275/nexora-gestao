"""Testes de ponta a ponta da API do NEXORA, rodando sobre um banco novo."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "SenhaDeTeste123"


@pytest.fixture(scope="module")
def app_module():
    for name in ("loja.db", "loja.db-journal", "loja.db-wal", "loja.db-shm"):
        (ROOT / name).unlink(missing_ok=True)
    os.environ["LOJA_INITIAL_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    sys.path.insert(0, str(ROOT))
    module = importlib.import_module("app")
    yield module
    (ROOT / "loja.db").unlink(missing_ok=True)


@pytest.fixture(scope="module")
def client(app_module):
    return TestClient(app_module.app)


@pytest.fixture(scope="module")
def admin(client):
    response = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert response.status_code == 200
    return client


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_rotas_protegidas_exigem_login(app_module):
    anonymous = TestClient(app_module.app)
    assert anonymous.get("/api/me").status_code == 401
    assert anonymous.get("/api/products").status_code == 401


def test_login_com_senha_errada(app_module):
    anonymous = TestClient(app_module.app)
    response = anonymous.post("/api/login", json={"username": "admin", "password": "errada"})
    assert response.status_code == 401


def test_admin_inicial_pertence_a_uma_empresa(admin):
    me = admin.get("/api/me").json()
    assert me["username"] == "admin"
    assert me["role"] == "admin"
    assert me["company_id"] is not None


def test_cadastro_de_produto(admin):
    response = admin.post(
        "/api/products",
        json={"name": "Refrigerante 2L", "price": 12, "cost": 7, "stock": 10},
    )
    assert response.status_code == 200
    products = admin.get("/api/products").json()
    assert any(p["name"] == "Refrigerante 2L" and p["stock"] == 10 for p in products)


def test_venda_baixa_estoque_e_calcula_total(admin):
    product = next(p for p in admin.get("/api/products").json() if p["name"] == "Refrigerante 2L")

    assert admin.post("/api/cash/open", json={"opening_amount": 50}).status_code == 200

    response = admin.post(
        "/api/sales",
        json={"items": [{"product_id": product["id"], "qty": 2}], "payment_method": "PIX"},
    )
    assert response.status_code == 200

    sale = admin.get("/api/sales").json()
    sales = sale if isinstance(sale, list) else sale.get("items", sale.get("sales", []))
    assert any(abs(s["total"] - 24) < 0.01 for s in sales)

    updated = next(p for p in admin.get("/api/products").json() if p["id"] == product["id"])
    assert updated["stock"] == 8


def test_logout_encerra_sessao(app_module):
    session = TestClient(app_module.app)
    assert session.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    assert session.get("/api/me").status_code == 200
    session.post("/api/logout")
    assert session.get("/api/me").status_code == 401
