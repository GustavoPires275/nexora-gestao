"""
MASTER ULTIMATE
Camada superior de administração do SaaS.

Não substitui MASTER ENTERPRISE, MASTER PLUS ou SUPER MASTER.
Funciona como camada adicional.
"""

from datetime import datetime


MASTER_ULTIMATE_VERSION = "1.0.0"


def get_master_ultimate_info():
    return {
        "name": "MASTER ULTIMATE",
        "version": MASTER_ULTIMATE_VERSION,
        "status": "active",
        "modules": [
            "overview",
            "companies",
            "users",
            "financial",
            "monitoring",
            "audit",
            "security",
            "system",
            "reports",
            "global_search",
        ],
        "generated_at": datetime.now().isoformat(),
    }


def register_master_ultimate(app):
    """
    Registro seguro da camada MASTER ULTIMATE.

    A função é idempotente:
    chamar mais de uma vez não deve quebrar o sistema.
    """

    if getattr(app.state, "master_ultimate_registered", False):
        return

    app.state.master_ultimate_registered = True

    print(
        "[MASTER ULTIMATE] camada superior registrada | "
        f"version={MASTER_ULTIMATE_VERSION}"
    )


__all__ = [
    "MASTER_ULTIMATE_VERSION",
    "get_master_ultimate_info",
    "register_master_ultimate",
]
