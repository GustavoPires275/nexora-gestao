
(() => {
    "use strict";

    if (window.__MASTER_CONTROL_EXPANSION__) {
        console.info("[MASTER EXPANSION] já carregado.");
        return;
    }

    window.__MASTER_CONTROL_EXPANSION__ = true;

    const API = async (url, options = {}) => {
        const opts = {
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {})
            },
            ...options
        };

        const response = await fetch(url, opts);

        let data = {};
        try {
            data = await response.json();
        } catch (_) {
            data = {
                ok: false,
                error: "Resposta inválida do servidor."
            };
        }

        if (!response.ok || data.ok === false) {
            throw new Error(data.error || data.message || `HTTP ${response.status}`);
        }

        return data;
    };

    const esc = value => String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

    const money = value => {
        const n = Number(value || 0);
        return n.toLocaleString("pt-BR", {
            style: "currency",
            currency: "BRL"
        });
    };

    const notify = (message, error = false) => {
        let box = document.getElementById("master-expansion-toast");

        if (!box) {
            box = document.createElement("div");
            box.id = "master-expansion-toast";
            document.body.appendChild(box);
        }

        box.textContent = message;
        box.className = error
            ? "master-exp-toast master-exp-error"
            : "master-exp-toast master-exp-ok";

        clearTimeout(window.__masterToastTimer);

        window.__masterToastTimer = setTimeout(() => {
            box.className = "";
        }, 3500);
    };

    const style = document.createElement("style");

    style.textContent = `
        #master-expansion-button {
            position: fixed;
            right: 22px;
            bottom: 22px;
            z-index: 99990;
            border: 1px solid #cfe0d2;
            background: #fff;
            color: #285e40;
            padding: 13px 17px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: 700;
            box-shadow: 0 12px 35px rgba(23,55,34,.16);
            transition: .2s ease;
        }

        #master-expansion-button:hover {
            transform: translateY(-2px);
            border-color: #79a986;
        }

        #master-expansion-overlay {
            display: none;
            position: fixed;
            inset: 0;
            z-index: 99999;
            background: rgba(0,0,0,.72);
            backdrop-filter: blur(5px);
            align-items: center;
            justify-content: center;
            padding: 24px;
        }

        #master-expansion-panel {
            width: min(1180px, 96vw);
            max-height: 92vh;
            overflow: auto;
            background: #f4f7f5;
            color: #1b2820;
            border: 1px solid #dce7df;
            border-radius: 16px;
            box-shadow: 0 30px 100px rgba(15,38,23,.3);
            padding: 22px;
        }

        .master-exp-head {
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:16px;
            margin-bottom:18px;
        }

        .master-exp-head h2 {
            margin:0;
            font-size:22px;
        }

        .master-exp-head p {
            margin:5px 0 0;
            color:#65736a;
            font-size:13px;
        }

        .master-exp-close {
            border:0;
            background:#edf4ee;
            color:#285e40;
            border-radius:9px;
            width:38px;
            height:38px;
            cursor:pointer;
            font-size:20px;
        }

        .master-exp-tabs {
            display:flex;
            gap:8px;
            flex-wrap:wrap;
            margin-bottom:18px;
        }

        .master-exp-tabs button {
            border:1px solid #dce7df;
            background:#fff;
            color:#405047;
            padding:9px 13px;
            border-radius:9px;
            cursor:pointer;
        }

        .master-exp-tabs button.active {
            background:#e9f3eb;
            border-color:#a9cbb0;
            color:#285e40;
        }

        .master-exp-section {
            background:#fff;
            border:1px solid #e3ebe5;
            border-radius:13px;
            padding:16px;
            margin-bottom:14px;
        }

        .master-exp-section h3 {
            margin:0 0 13px;
            font-size:16px;
        }

        .master-exp-grid {
            display:grid;
            grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
            gap:10px;
        }

        .master-exp-grid input,
        .master-exp-grid select,
        .master-exp-grid textarea {
            width:100%;
            box-sizing:border-box;
            border:1px solid #d6e1d8;
            background:#fff;
            color:#1b2820;
            padding:10px;
            border-radius:8px;
            outline:none;
            min-width:0;
        }

        .master-exp-grid textarea {
            min-height:80px;
            resize:vertical;
        }

        .master-exp-actions {
            display:flex;
            gap:8px;
            flex-wrap:wrap;
            margin-top:12px;
        }

        .master-exp-actions button {
            border:1px solid #cfe0d2;
            background:#edf5ee;
            color:#285e40;
            padding:9px 12px;
            border-radius:8px;
            cursor:pointer;
        }

        .master-exp-actions button:hover {
            background:#e1efe4;
        }

        .master-exp-danger {
            background:#fbefee !important;
            border-color:#edd1cf !important;
            color:#933632 !important;
        }

        .master-exp-success {
            background:#e8f3e9 !important;
            color:#285e40 !important;
        }

        .master-exp-list {
            display:grid;
            gap:9px;
        }

        .master-exp-row {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            padding:12px;
            border:1px solid #e3ebe5;
            border-radius:10px;
            background:#fff;
        }

        .master-exp-row-main {
            min-width:0;
        }

        .master-exp-row-title {
            font-weight:700;
        }

        .master-exp-row-sub {
            font-size:12px;
            color:#65736a;
            margin-top:3px;
        }

        .master-exp-row-actions {
            display:flex;
            gap:6px;
            flex-wrap:wrap;
            justify-content:flex-end;
        }

        .master-exp-row-actions button {
            border:1px solid #dce7df;
            background:#f6f9f6;
            color:#304237;
            border-radius:7px;
            padding:7px 9px;
            cursor:pointer;
        }

        .master-exp-empty {
            color:#65736a;
            padding:18px;
            text-align:center;
        }

        .master-exp-toast {
            position:fixed;
            right:22px;
            bottom:78px;
            z-index:100000;
            padding:12px 15px;
            border-radius:10px;
            box-shadow:0 12px 35px rgba(0,0,0,.18);
            font-weight:600;
        }

        .master-exp-ok {
            background:#14583c;
            color:#fff;
        }

        .master-exp-error {
            background:#982f2d;
            color:#fff;
        }

        .master-exp-stat {
            padding:14px;
            border-radius:10px;
            background:#f8faf8;
            border:1px solid #e3ebe5;
        }

        .master-exp-stat strong {
            display:block;
            font-size:20px;
            margin-top:5px;
        }

        @media(max-width:700px) {
            #master-expansion-overlay {
                padding:8px;
            }

            #master-expansion-panel {
                width:100%;
                max-height:96vh;
                padding:14px;
            }

            .master-exp-row {
                align-items:flex-start;
                flex-direction:column;
            }
        }
    `;

    document.head.appendChild(style);

    // ------------------------------------------------------------
    // HTML BASE
    // ------------------------------------------------------------

    const button = document.createElement("button");
    button.id = "master-expansion-button";
    button.innerHTML = "⚙ MASTER ACTIONS";

    const overlay = document.createElement("div");
    overlay.id = "master-expansion-overlay";

    overlay.innerHTML = `
        <div id="master-expansion-panel">
            <div class="master-exp-head">
                <div>
                    <h2>MASTER CONTROL</h2>
                    <p>Central de operações — Enterprise Platform Control</p>
                </div>
                <button class="master-exp-close" id="master-exp-close">×</button>
            </div>

            <div class="master-exp-tabs">
                <button data-exp-tab="companies" class="active">Empresas</button>
                <button data-exp-tab="users">Usuários</button>
                <button data-exp-tab="security">Segurança</button>
                <button data-exp-tab="support">Suporte</button>
                <button data-exp-tab="billing">Billing</button>
                <button data-exp-tab="flags">Feature Flags</button>
                <button data-exp-tab="system">Sistema</button>
                <button data-exp-tab="integrations">Integrações</button>
            </div>

            <div id="master-exp-content"></div>
        </div>
    `;

    document.body.appendChild(button);
    document.body.appendChild(overlay);

    const content = document.getElementById("master-exp-content");

    const open = (tab) => {
        const name = typeof tab === "string" && tab ? tab : "companies";
        overlay.style.display = "flex";
        document.querySelectorAll("[data-exp-tab]").forEach(x => {
            x.classList.toggle("active", x.dataset.expTab === name);
        });
        loadTab(name);
    };

    const close = () => {
        overlay.style.display = "none";
    };

    button.addEventListener("click", open);
    document.getElementById("master-exp-close").addEventListener("click", close);

    overlay.addEventListener("click", e => {
        if (e.target === overlay) close();
    });

    document.addEventListener("keydown", e => {
        if (e.key === "Escape") close();
    });

    document.querySelectorAll("[data-exp-tab]").forEach(tab => {
        tab.addEventListener("click", () => {
            document.querySelectorAll("[data-exp-tab]").forEach(x => x.classList.remove("active"));
            tab.classList.add("active");
            loadTab(tab.dataset.expTab);
        });
    });

    const unwrap = data => {
        if (!data) return [];
        if (Array.isArray(data)) return data;
        if (Array.isArray(data.data)) return data.data;
        if (Array.isArray(data.companies)) return data.companies;
        if (Array.isArray(data.users)) return data.users;
        if (Array.isArray(data.rows)) return data.rows;
        return [];
    };

    const renderLoading = () => {
        content.innerHTML = `
            <div class="master-exp-section">
                <div class="master-exp-empty">Carregando dados do MASTER...</div>
            </div>
        `;
    };

    // ------------------------------------------------------------
    // EMPRESAS
    // ------------------------------------------------------------

    async function companiesPage() {
        renderLoading();

        try {
            const result = await API("/master/enterprise/api/companies");
            // a rota Enterprise devolve "status" (não "active"): deriva o booleano usado nos botões
            const companies = unwrap(result).map(c => ({
                ...c,
                active: c.active !== undefined
                    ? !!c.active
                    : !["0", "false", "inativa", "inativo", "suspensa", "suspenso"]
                        .includes(String(c.status ?? "").toLowerCase().trim())
            }));

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Nova empresa</h3>

                    <div class="master-exp-grid">
                        <input id="exp-company-name" placeholder="Nome da empresa">
                        <select id="exp-company-segment" aria-label="Segmento da empresa">
                            <option value="">Selecione o segmento</option>
                            <option value="LOJA">Loja</option>
                            <option value="RESTAURANTE">Restaurante</option>
                        </select>
                    </div>

                    <div class="master-exp-actions">
                        <button class="master-exp-success" id="exp-company-create">
                            Criar empresa
                        </button>
                        <button id="exp-company-refresh">
                            Atualizar
                        </button>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Empresas cadastradas</h3>
                    <div class="master-exp-list">
                        ${
                            companies.length
                            ? companies.map(company => `
                                <div class="master-exp-row">
                                    <div class="master-exp-row-main">
                                        <div class="master-exp-row-title">
                                            ${esc(company.name || company.company_name || `Empresa #${company.id}`)}
                                        </div>
                                        <div class="master-exp-row-sub">
                                            ID: ${esc(company.id)}
                                            · Status: ${company.active ? "Ativa" : "Suspensa"}
                                            · Segmento: ${esc(company.segment || "LOJA")}
                                        </div>
                                    </div>

                                    <div class="master-exp-row-actions">
                                        <button data-company-detail="${company.id}">Detalhes</button>
                                        <button data-company-edit="${company.id}">Editar</button>
                                        ${
                                            company.active
                                            ? `<button class="master-exp-danger" data-company-suspend="${company.id}">Suspender</button>`
                                            : `<button class="master-exp-success" data-company-activate="${company.id}">Ativar</button>`
                                        }
                                        <button data-company-support="${company.id}">Suporte</button>
                                        <button class="master-exp-danger" data-company-revoke="${company.id}">
                                            Encerrar sessões
                                        </button>
                                    </div>
                                </div>
                            `).join("")
                            : `<div class="master-exp-empty">Nenhuma empresa encontrada.</div>`
                        }
                    </div>
                </div>
            `;

            document.getElementById("exp-company-create")
                .addEventListener("click", createCompany);

            document.getElementById("exp-company-refresh")
                .addEventListener("click", companiesPage);

            document.querySelectorAll("[data-company-detail]")
                .forEach(x => x.addEventListener("click", () => companyDetail(x.dataset.companyDetail)));

            document.querySelectorAll("[data-company-edit]")
                .forEach(x => x.addEventListener("click", () => editCompany(x.dataset.companyEdit)));

            document.querySelectorAll("[data-company-suspend]")
                .forEach(x => x.addEventListener("click", () => companyStatus(x.dataset.companySuspend, false)));

            document.querySelectorAll("[data-company-activate]")
                .forEach(x => x.addEventListener("click", () => companyStatus(x.dataset.companyActivate, true)));

            document.querySelectorAll("[data-company-support]")
                .forEach(x => x.addEventListener("click", () => startSupport(x.dataset.companySupport)));

            document.querySelectorAll("[data-company-revoke]")
                .forEach(x => x.addEventListener("click", () => revokeCompanySessions(x.dataset.companyRevoke)));

        } catch (error) {
            content.innerHTML = `
                <div class="master-exp-section">
                    <div class="master-exp-empty">${esc(error.message)}</div>
                </div>
            `;
        }
    }

    async function createCompany() {
        const name = document.getElementById("exp-company-name").value.trim();
        const segment = document.getElementById("exp-company-segment").value;

        if (!name) {
            notify("Informe o nome da empresa.", true);
            return;
        }
        if (!["LOJA", "RESTAURANTE"].includes(segment)) {
            notify("Selecione Loja ou Restaurante.", true);
            return;
        }

        try {
            await API("/master/plus/api/company/create", {
                method: "POST",
                body: JSON.stringify({ name, segment })
            });

            notify("Empresa criada com sucesso.");
            companiesPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function companyDetail(id) {
        try {
            const [detail, plus] = await Promise.all([
                API(`/master/api/companies/${id}`),
                API(`/master/plus/api/company/${id}`)
            ]);

            const data = plus.data || detail.data || detail;

            const users = Array.isArray(data.users) ? data.users : [];

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>
                        ${esc(data.name || data.company?.name || `Empresa #${id}`)}
                    </h3>

                    <div class="master-exp-grid">
                        <div class="master-exp-stat">
                            Plano
                            <strong>${esc(data.plan || data.saas?.plan || "standard")}</strong>
                        </div>

                        <div class="master-exp-stat">
                            Mensalidade
                            <strong>${money(data.monthly_price || data.saas?.monthly_price)}</strong>
                        </div>

                        <div class="master-exp-stat">
                            Usuários
                            <strong>${users.length}</strong>
                        </div>

                        <div class="master-exp-stat">
                            Status
                            <strong>${data.active === false ? "Suspensa" : "Ativa"}</strong>
                        </div>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Plano e limites</h3>

                    <div class="master-exp-grid">
                        <input id="exp-plan" value="${esc(data.plan || "standard")}" placeholder="Plano">

                        <input id="exp-price"
                            type="number"
                            step="0.01"
                            value="${Number(data.monthly_price || 0)}"
                            placeholder="Mensalidade">

                        <input id="exp-max-users"
                            type="number"
                            value="${Number(data.max_users || 0)}"
                            placeholder="Máx. usuários">

                        <input id="exp-max-products"
                            type="number"
                            value="${Number(data.max_products || 0)}"
                            placeholder="Máx. produtos">

                        <input id="exp-max-customers"
                            type="number"
                            value="${Number(data.max_customers || 0)}"
                            placeholder="Máx. clientes">

                        <input id="exp-max-sales"
                            type="number"
                            value="${Number(data.max_sales_month || 0)}"
                            placeholder="Máx. vendas/mês">

                        <input id="exp-max-storage"
                            type="number"
                            value="${Number(data.max_storage_mb || 0)}"
                            placeholder="Máx. armazenamento MB">
                    </div>

                    <div class="master-exp-actions">
                        <button id="exp-save-plan" class="master-exp-success">
                            Salvar plano
                        </button>

                        <button id="exp-save-limits">
                            Salvar limites
                        </button>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Usuários da empresa</h3>

                    <div class="master-exp-list">
                        ${
                            users.length
                            ? users.map(user => `
                                <div class="master-exp-row">
                                    <div class="master-exp-row-main">
                                        <div class="master-exp-row-title">
                                            ${esc(user.name || user.username)}
                                        </div>

                                        <div class="master-exp-row-sub">
                                            @${esc(user.username)}
                                            · ${esc(user.role)}
                                            · ${user.active ? "Ativo" : "Inativo"}
                                        </div>
                                    </div>

                                    <div class="master-exp-row-actions">
                                        <button data-user-detail-toggle="${user.id}">
                                            ${user.active ? "Desativar" : "Ativar"}
                                        </button>

                                        <button data-user-detail-role="${user.id}">
                                            Cargo
                                        </button>

                                        <button data-user-detail-logout="${user.id}">
                                            Logout
                                        </button>
                                    </div>
                                </div>
                            `).join("")
                            : `<div class="master-exp-empty">Nenhum usuário.</div>`
                        }
                    </div>
                </div>

                <div class="master-exp-section">
                    <div class="master-exp-actions">
                        <button id="exp-back-companies">← Voltar para empresas</button>
                    </div>
                </div>
            `;

            document.getElementById("exp-save-plan")
                .addEventListener("click", () => savePlan(id));

            document.getElementById("exp-save-limits")
                .addEventListener("click", () => saveLimits(id));

            document.getElementById("exp-back-companies")
                .addEventListener("click", companiesPage);

            document.querySelectorAll("[data-user-detail-toggle]")
                .forEach(x => x.addEventListener("click", () => toggleUser(x.dataset.userDetailToggle)));

            document.querySelectorAll("[data-user-detail-role]")
                .forEach(x => x.addEventListener("click", () => changeUserRole(x.dataset.userDetailRole)));

            document.querySelectorAll("[data-user-detail-logout]")
                .forEach(x => x.addEventListener("click", () => logoutUser(x.dataset.userDetailLogout)));

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function editCompany(id) {
        try {
            const result = await API(`/master/api/companies/${id}`);
            const company = result.company || result.data?.company || result;
            const segment = String(company.segment || "LOJA").toUpperCase();
            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Editar ${esc(company.name || `Empresa #${id}`)}</h3>
                    <div class="master-exp-grid">
                        <input id="exp-edit-company-name" value="${esc(company.name || "")}" placeholder="Nome da empresa">
                        <select id="exp-edit-company-segment" aria-label="Segmento da empresa">
                            <option value="LOJA" ${segment === "LOJA" ? "selected" : ""}>Loja</option>
                            <option value="RESTAURANTE" ${segment === "RESTAURANTE" ? "selected" : ""}>Restaurante</option>
                        </select>
                    </div>
                    <div class="master-exp-actions">
                        <button id="exp-save-company" class="master-exp-success">Salvar</button>
                        <button id="exp-cancel-company">Voltar</button>
                    </div>
                </div>`;
            document.getElementById("exp-save-company").addEventListener("click", async () => {
                const name = document.getElementById("exp-edit-company-name").value.trim();
                const nextSegment = document.getElementById("exp-edit-company-segment").value;
                if (!name || !["LOJA", "RESTAURANTE"].includes(nextSegment)) {
                    notify("Informe o nome e selecione um segmento valido.", true);
                    return;
                }
                try {
                    await API(`/master/api/companies/${id}`, {
                        method: "PUT",
                        body: JSON.stringify({ name, segment: nextSegment })
                    });
                    notify("Empresa atualizada.");
                    companiesPage();
                } catch (error) { notify(error.message, true); }
            });
            document.getElementById("exp-cancel-company").addEventListener("click", companiesPage);
        } catch (error) {
            notify(error.message, true);
        }
    }

    async function companyStatus(id, active) {
        const action = active ? "reativar" : "suspender";

        if (!confirm(`Deseja ${action} esta empresa?`)) return;

        try {
            await API(`/master/api/companies/${id}/status`, {
                method: "POST",
                body: JSON.stringify({ active })
            });

            notify(active ? "Empresa ativada." : "Empresa suspensa.");
            companiesPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function revokeCompanySessions(id) {
        if (!confirm("Encerrar todas as sessões desta empresa?")) return;

        try {
            const result = await API(`/master/api/companies/${id}/sessions/revoke`, {
                method: "POST"
            });

            notify(`${result.deleted || 0} sessão(ões) encerrada(s).`);

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function savePlan(id) {
        try {
            await API(`/master/plus/api/company/${id}/plan`, {
                method: "POST",
                body: JSON.stringify({
                    plan: document.getElementById("exp-plan").value.trim(),
                    monthly_price: Number(document.getElementById("exp-price").value || 0),
                    billing_status: "active"
                })
            });

            notify("Plano atualizado.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function saveLimits(id) {
        try {
            await API(`/master/plus/api/company/${id}/limits`, {
                method: "POST",
                body: JSON.stringify({
                    max_users: Number(document.getElementById("exp-max-users").value || 0),
                    max_products: Number(document.getElementById("exp-max-products").value || 0),
                    max_customers: Number(document.getElementById("exp-max-customers").value || 0),
                    max_sales_month: Number(document.getElementById("exp-max-sales").value || 0),
                    max_storage_mb: Number(document.getElementById("exp-max-storage").value || 0)
                })
            });

            notify("Limites atualizados.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // USUÁRIOS
    // ------------------------------------------------------------

    async function usersPage() {
        renderLoading();

        try {
            const result = await API("/master/enterprise/api/users");
            const users = unwrap(result);

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Criar administrador</h3>

                    <div class="master-exp-grid">
                        <input id="exp-admin-company" type="number" placeholder="ID da empresa">
                        <input id="exp-admin-name" placeholder="Nome">
                        <input id="exp-admin-username" placeholder="Usuário">
                        <input id="exp-admin-password" type="password" placeholder="Senha (6+ caracteres)">
                    </div>

                    <div class="master-exp-actions">
                        <button class="master-exp-success" id="exp-create-admin">
                            Criar administrador
                        </button>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Usuários globais</h3>

                    <div class="master-exp-list">
                        ${
                            users.length
                            ? users.map(user => `
                                <div class="master-exp-row">
                                    <div class="master-exp-row-main">
                                        <div class="master-exp-row-title">
                                            ${esc(user.name || user.username)}
                                        </div>

                                        <div class="master-exp-row-sub">
                                            ID ${esc(user.id)}
                                            · @${esc(user.username)}
                                            · ${esc(user.role)}
                                            · Empresa ${esc(user.company_id)}
                                            · ${user.active ? "Ativo" : "Inativo"}
                                        </div>
                                    </div>

                                    <div class="master-exp-row-actions">
                                        <button data-u-toggle="${user.id}">
                                            ${user.active ? "Desativar" : "Ativar"}
                                        </button>

                                        <button data-u-role="${user.id}">
                                            Cargo
                                        </button>

                                        <button data-u-password="${user.id}">
                                            Reset senha
                                        </button>

                                        <button data-u-logout="${user.id}">
                                            Logout
                                        </button>
                                    </div>
                                </div>
                            `).join("")
                            : `<div class="master-exp-empty">Nenhum usuário encontrado.</div>`
                        }
                    </div>
                </div>
            `;

            document.getElementById("exp-create-admin")
                .addEventListener("click", createAdmin);

            document.querySelectorAll("[data-u-toggle]")
                .forEach(x => x.addEventListener("click", () => toggleUser(x.dataset.uToggle)));

            document.querySelectorAll("[data-u-role]")
                .forEach(x => x.addEventListener("click", () => changeUserRole(x.dataset.uRole)));

            document.querySelectorAll("[data-u-password]")
                .forEach(x => x.addEventListener("click", () => resetPassword(x.dataset.uPassword)));

            document.querySelectorAll("[data-u-logout]")
                .forEach(x => x.addEventListener("click", () => logoutUser(x.dataset.uLogout)));

        } catch (error) {
            content.innerHTML = `
                <div class="master-exp-section">
                    <div class="master-exp-empty">${esc(error.message)}</div>
                </div>
            `;
        }
    }

    async function createAdmin() {
        const payload = {
            company_id: Number(document.getElementById("exp-admin-company").value),
            name: document.getElementById("exp-admin-name").value.trim(),
            username: document.getElementById("exp-admin-username").value.trim(),
            password: document.getElementById("exp-admin-password").value
        };

        if (!payload.company_id || !payload.name || !payload.username || payload.password.length < 6) {
            notify("Preencha empresa, nome, usuário e senha de 6+ caracteres.", true);
            return;
        }

        try {
            await API("/master/api/users/create-admin", {
                method: "POST",
                body: JSON.stringify(payload)
            });

            notify("Administrador criado.");
            usersPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function toggleUser(id) {
        if (!confirm("Alterar o status deste usuário?")) return;

        try {
            const result = await API(`/master/api/users/${id}/toggle`, {
                method: "POST"
            });

            notify(result.active ? "Usuário ativado." : "Usuário desativado.");
            usersPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function changeUserRole(id) {
        const role = prompt(
            "Novo cargo:\nadmin\ngerente\nvendedor\ncaixa"
        );

        if (role === null) return;

        const normalized = role.trim().toLowerCase();

        if (!["admin", "gerente", "vendedor", "caixa"].includes(normalized)) {
            notify("Cargo inválido.", true);
            return;
        }

        try {
            await API(`/master/api/users/${id}/role`, {
                method: "POST",
                body: JSON.stringify({ role: normalized })
            });

            notify("Cargo atualizado.");
            usersPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function resetPassword(id) {
        const password = prompt("Nova senha (mínimo 6 caracteres):");

        if (password === null) return;

        if (password.length < 6) {
            notify("A senha precisa ter pelo menos 6 caracteres.", true);
            return;
        }

        try {
            await API(`/master/api/users/${id}/reset-password`, {
                method: "POST",
                body: JSON.stringify({ password })
            });

            notify("Senha redefinida.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function logoutUser(id) {
        if (!confirm("Encerrar todas as sessões deste usuário?")) return;

        try {
            const result = await API(`/master/api/users/${id}/force-logout`, {
                method: "POST"
            });

            notify(`${result.deleted || 0} sessão(ões) encerrada(s).`);

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // SEGURANÇA
    // ------------------------------------------------------------

    async function securityPage() {
        renderLoading();

        try {
            const sessions = await API("/master/enterprise/api/security");

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Segurança global</h3>

                    <div class="master-exp-grid">
                        <div class="master-exp-stat">
                            Status
                            <strong>MASTER protegido</strong>
                        </div>

                        <div class="master-exp-stat">
                            Sessões
                            <strong>${esc(
                                sessions.active_sessions ??
                                sessions.sessions ??
                                sessions.data?.active_sessions ??
                                "—"
                            )}</strong>
                        </div>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Ações críticas</h3>

                    <div class="master-exp-actions">
                        <button class="master-exp-danger" id="exp-global-logout">
                            Encerrar todas as sessões globais
                        </button>

                        <button id="exp-security-refresh">
                            Atualizar segurança
                        </button>
                    </div>
                </div>
            `;

            document.getElementById("exp-global-logout")
                .addEventListener("click", globalLogout);

            document.getElementById("exp-security-refresh")
                .addEventListener("click", securityPage);

        } catch (error) {
            content.innerHTML = `
                <div class="master-exp-section">
                    <div class="master-exp-empty">${esc(error.message)}</div>
                </div>
            `;
        }
    }

    async function globalLogout() {
        if (!confirm(
            "ATENÇÃO: isso encerrará as sessões de todos os usuários, exceto a sessão MASTER atual. Continuar?"
        )) return;

        try {
            const result = await API("/master/api/sessions/global-revoke", {
                method: "POST"
            });

            notify(`${result.deleted || 0} sessão(ões) encerrada(s).`);

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // SUPORTE
    // ------------------------------------------------------------

    async function supportPage() {
        renderLoading();

        try {
            let current = {};

            try {
                current = await API("/master/plus/api/support/current");
            } catch (_) {}

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Modo de suporte</h3>

                    <p>
                        O modo suporte permite iniciar uma sessão controlada
                        para atendimento de uma empresa.
                    </p>

                    <div class="master-exp-grid">
                        <input id="exp-support-company"
                            type="number"
                            placeholder="ID da empresa">

                        <input id="exp-support-reason"
                            placeholder="Motivo do suporte"
                            value="Atendimento técnico">
                    </div>

                    <div class="master-exp-actions">
                        <button class="master-exp-success" id="exp-support-start">
                            Iniciar suporte
                        </button>

                        <button class="master-exp-danger" id="exp-support-stop">
                            Encerrar suporte
                        </button>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Sessão atual</h3>

                    <pre style="white-space:pre-wrap;opacity:.75">${esc(
                        JSON.stringify(current, null, 2)
                    )}</pre>
                </div>
            `;

            document.getElementById("exp-support-start")
                .addEventListener("click", () => {
                    startSupport(
                        document.getElementById("exp-support-company").value
                    );
                });

            document.getElementById("exp-support-stop")
                .addEventListener("click", stopSupport);

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function startSupport(id) {
        if (!id) {
            notify("Informe o ID da empresa.", true);
            return;
        }

        const reason = prompt("Motivo do suporte:", "Atendimento técnico");

        if (reason === null) return;

        try {
            const result = await API(`/master/plus/api/support/start/${id}`, {
                method: "POST",
                body: JSON.stringify({ reason })
            });

            notify(`Suporte iniciado: ${result.company_name || "empresa"}.`);
            supportPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function stopSupport() {
        try {
            await API("/master/plus/api/support/stop", {
                method: "POST"
            });

            notify("Modo suporte encerrado.");
            supportPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // BILLING
    // ------------------------------------------------------------

    async function billingPage() {
        content.innerHTML = `
            <div class="master-exp-section">
                <h3>Registrar cobrança</h3>

                <div class="master-exp-grid">
                    <input id="exp-bill-company" type="number" placeholder="ID da empresa">
                    <input id="exp-bill-amount" type="number" step="0.01" placeholder="Valor">
                    <input id="exp-bill-reference" placeholder="Referência">
                    <select id="exp-bill-status">
                        <option value="pending">Pendente</option>
                        <option value="paid">Pago</option>
                        <option value="cancelled">Cancelado</option>
                    </select>
                    <input id="exp-bill-due" type="date" placeholder="Vencimento">
                    <input id="exp-bill-paid" type="date" placeholder="Pagamento">
                </div>

                <div class="master-exp-actions">
                    <button class="master-exp-success" id="exp-bill-save">
                        Registrar cobrança
                    </button>
                </div>
            </div>

            <div class="master-exp-section">
                <h3>Billing / SaaS</h3>
                <div class="master-exp-actions">
                    <button id="exp-billing-data">
                        Atualizar dados do Billing
                    </button>
                </div>
            </div>
        `;

        document.getElementById("exp-bill-save")
            .addEventListener("click", saveBilling);

        document.getElementById("exp-billing-data")
            .addEventListener("click", async () => {
                try {
                    const result = await API("/master/enterprise/api/billing");
                    alert(JSON.stringify(result, null, 2));
                } catch (error) {
                    notify(error.message, true);
                }
            });
    }

    async function saveBilling() {
        const payload = {
            company_id: Number(document.getElementById("exp-bill-company").value),
            amount: Number(document.getElementById("exp-bill-amount").value || 0),
            status: document.getElementById("exp-bill-status").value,
            reference: document.getElementById("exp-bill-reference").value.trim(),
            due_date: document.getElementById("exp-bill-due").value || null,
            paid_at: document.getElementById("exp-bill-paid").value || null
        };

        if (!payload.company_id) {
            notify("Informe o ID da empresa.", true);
            return;
        }

        try {
            await API("/master/plus/api/billing/history", {
                method: "POST",
                body: JSON.stringify(payload)
            });

            notify("Cobrança registrada.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // FEATURE FLAGS
    // ------------------------------------------------------------

    async function flagsPage() {
        content.innerHTML = `
            <div class="master-exp-section">
                <h3>Feature Flag por empresa</h3>

                <div class="master-exp-grid">
                    <input id="exp-flag-company" type="number" placeholder="ID da empresa">
                    <input id="exp-flag-name" placeholder="Nome da feature">

                    <select id="exp-flag-enabled">
                        <option value="true">Ativada</option>
                        <option value="false">Desativada</option>
                    </select>
                </div>

                <div class="master-exp-actions">
                    <button class="master-exp-success" id="exp-flag-save">
                        Salvar Feature Flag
                    </button>
                </div>
            </div>

            <div class="master-exp-section">
                <h3>Flags atuais da empresa</h3>

                <div class="master-exp-grid">
                    <input id="exp-flag-view-company"
                        type="number"
                        placeholder="ID da empresa">

                    <button id="exp-flag-view">
                        Consultar
                    </button>
                </div>

                <pre id="exp-flag-output"
                    style="white-space:pre-wrap;opacity:.75"></pre>
            </div>
        `;

        document.getElementById("exp-flag-save")
            .addEventListener("click", saveFlag);

        document.getElementById("exp-flag-view")
            .addEventListener("click", viewFlags);
    }

    async function saveFlag() {
        const company_id = Number(document.getElementById("exp-flag-company").value);
        const feature = document.getElementById("exp-flag-name").value.trim();
        const enabled = document.getElementById("exp-flag-enabled").value === "true";

        if (!company_id || !feature) {
            notify("Empresa e feature são obrigatórias.", true);
            return;
        }

        try {
            await API(`/master/plus/api/flag/${company_id}`, {
                method: "POST",
                body: JSON.stringify({ feature, enabled })
            });

            notify("Feature Flag atualizada.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function viewFlags() {
        const id = Number(document.getElementById("exp-flag-view-company").value);

        if (!id) {
            notify("Informe o ID da empresa.", true);
            return;
        }

        try {
            const result = await API(`/master/plus/api/flags/${id}`);

            document.getElementById("exp-flag-output").textContent =
                JSON.stringify(result, null, 2);

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // SISTEMA
    // ------------------------------------------------------------

    async function systemPage() {
        renderLoading();

        try {
            const [system, diagnostics, maintenance] = await Promise.all([
                API("/master/enterprise/api/system"),
                API("/master/enterprise/api/diagnostics"),
                API("/master/api/maintenance")
            ]);

            content.innerHTML = `
                <div class="master-exp-section">
                    <h3>Sistema</h3>

                    <div class="master-exp-grid">
                        <div class="master-exp-stat">
                            Sistema
                            <strong>Online</strong>
                        </div>

                        <div class="master-exp-stat">
                            Manutenção
                            <strong>
                                ${maintenance?.enabled ? "ATIVA" : "INATIVA"}
                            </strong>
                        </div>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Modo manutenção</h3>

                    <div class="master-exp-grid">
                        <select id="exp-maint-enabled">
                            <option value="false"
                                ${maintenance?.enabled ? "" : "selected"}>
                                Desativada
                            </option>

                            <option value="true"
                                ${maintenance?.enabled ? "selected" : ""}>
                                Ativada
                            </option>
                        </select>

                        <input id="exp-maint-message"
                            value="${esc(
                                maintenance?.message ||
                                "Sistema em manutenção."
                            )}"
                            placeholder="Mensagem">
                    </div>

                    <div class="master-exp-actions">
                        <button class="master-exp-success" id="exp-maint-save">
                            Salvar manutenção
                        </button>
                    </div>
                </div>

                <div class="master-exp-section">
                    <h3>Diagnóstico</h3>

                    <pre style="white-space:pre-wrap;opacity:.72">${esc(
                        JSON.stringify(diagnostics, null, 2)
                    )}</pre>
                </div>

                <div class="master-exp-section">
                    <h3>Dados do sistema</h3>

                    <pre style="white-space:pre-wrap;opacity:.72">${esc(
                        JSON.stringify(system, null, 2)
                    )}</pre>
                </div>
            `;

            document.getElementById("exp-maint-save")
                .addEventListener("click", saveMaintenance);

        } catch (error) {
            content.innerHTML = `
                <div class="master-exp-section">
                    <div class="master-exp-empty">${esc(error.message)}</div>
                </div>
            `;
        }
    }

    async function saveMaintenance() {
        try {
            const enabled =
                document.getElementById("exp-maint-enabled").value === "true";

            const message =
                document.getElementById("exp-maint-message").value.trim();

            await API("/master/api/maintenance", {
                method: "POST",
                body: JSON.stringify({
                    enabled,
                    message
                })
            });

            notify("Manutenção atualizada.");
            systemPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // INTEGRAÇÕES
    // ------------------------------------------------------------

    async function integrationsPage() {
        content.innerHTML = `
            <div class="master-exp-section">
                <h3>API Key</h3>

                <div class="master-exp-grid">
                    <input id="exp-key-company" type="number" placeholder="ID da empresa">
                    <input id="exp-key-name" placeholder="Nome da chave" value="API Key">
                </div>

                <div class="master-exp-actions">
                    <button class="master-exp-success" id="exp-key-create">
                        Criar API Key
                    </button>
                </div>

                <pre id="exp-key-output"
                    style="white-space:pre-wrap;word-break:break-word"></pre>
            </div>

            <div class="master-exp-section">
                <h3>Webhook</h3>

                <div class="master-exp-grid">
                    <input id="exp-webhook-company" type="number" placeholder="ID da empresa">
                    <input id="exp-webhook-name" placeholder="Nome">
                    <input id="exp-webhook-url" placeholder="https://...">
                    <input id="exp-webhook-event" placeholder="Evento" value="*">
                </div>

                <div class="master-exp-actions">
                    <button class="master-exp-success" id="exp-webhook-create">
                        Criar Webhook
                    </button>
                </div>
            </div>

            <div class="master-exp-section">
                <h3>Integrações cadastradas</h3>

                <pre id="exp-integrations-output"
                    style="white-space:pre-wrap;opacity:.75">Carregando...</pre>
            </div>
        `;

        document.getElementById("exp-key-create")
            .addEventListener("click", createApiKey);

        document.getElementById("exp-webhook-create")
            .addEventListener("click", createWebhook);

        try {
            const result = await API("/master/plus/api/integrations");

            const out = document.getElementById("exp-integrations-output");
            if (out) out.textContent = JSON.stringify(result, null, 2);

        } catch (error) {
            const out = document.getElementById("exp-integrations-output");
            if (out) out.textContent = error.message;
        }
    }

    async function createApiKey() {
        const company_id = Number(document.getElementById("exp-key-company").value);
        const name = document.getElementById("exp-key-name").value.trim();

        if (!company_id) {
            notify("Informe o ID da empresa.", true);
            return;
        }

        try {
            const result = await API("/master/plus/api/integrations/key", {
                method: "POST",
                body: JSON.stringify({ company_id, name })
            });

            document.getElementById("exp-key-output").textContent =
                JSON.stringify(result, null, 2);

            notify("API Key criada. Salve a chave agora.");

        } catch (error) {
            notify(error.message, true);
        }
    }

    async function createWebhook() {
        const payload = {
            company_id: Number(document.getElementById("exp-webhook-company").value),
            name: document.getElementById("exp-webhook-name").value.trim(),
            url: document.getElementById("exp-webhook-url").value.trim(),
            event: document.getElementById("exp-webhook-event").value.trim() || "*"
        };

        if (!payload.company_id || !payload.url) {
            notify("Empresa e URL são obrigatórias.", true);
            return;
        }

        try {
            await API("/master/plus/api/integrations/webhook", {
                method: "POST",
                body: JSON.stringify(payload)
            });

            notify("Webhook criado.");
            integrationsPage();

        } catch (error) {
            notify(error.message, true);
        }
    }

    // ------------------------------------------------------------
    // COMPANY SUPPORT
    // ------------------------------------------------------------

    // ------------------------------------------------------------
    // ROUTER
    // ------------------------------------------------------------

    async function loadTab(tab) {
        switch (tab) {
            case "companies":
                return companiesPage();

            case "users":
                return usersPage();

            case "security":
                return securityPage();

            case "support":
                return supportPage();

            case "billing":
                return billingPage();

            case "flags":
                return flagsPage();

            case "system":
                return systemPage();

            case "integrations":
                return integrationsPage();

            default:
                return companiesPage();
        }
    }

    window.MasterControlExpansion = {
        open,
        close,
        loadTab,
        companiesPage,
        usersPage,
        securityPage
    };

    console.info("[MASTER EXPANSION] módulo carregado com sucesso.");
})();
