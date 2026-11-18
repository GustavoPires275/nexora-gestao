# -*- coding: utf-8 -*-
"""Master Enterprise - camada adicional não destrutiva."""
from __future__ import annotations
import base64, datetime as dt, hashlib, json, os, platform, secrets, sqlite3, time
from pathlib import Path

# Banco principal do sistema
DB_PATH = Path(__file__).resolve().parent / "loja.db"
from typing import Any
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_REGISTERED = False
_APP = None
_CONN = None
_GET_USER = None
_AUDIT = None
_NOW = None
_START = time.time()
ROOT = Path(__file__).resolve().parent
MAINT_FILE = ROOT / "master_maintenance.json"


def _json(v):
    try: return json.dumps(v, ensure_ascii=False, default=str)
    except Exception: return str(v)


def _conn():
    return _CONN()


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _cols(c, table):
    try: return {r[1] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except Exception: return set()


def _pick(c, table, candidates):
    cols = _cols(c, table)
    for x in candidates:
        if x in cols: return x
    return None


def _guard(request):
    try: u = _GET_USER(request)
    except Exception: u = None
    if not u or str(u.get("role", "")).lower() != "master":
        return JSONResponse({"ok": False, "error": "Acesso restrito ao Master"}, status_code=403)
    return None


def _master(request):
    try: return _GET_USER(request)
    except Exception: return None


def _audit(request, action, company_id=None, result="ok", details=None):
    try:
        u = _master(request) or {}
        if _AUDIT:
            _AUDIT(action, company_id=company_id, user_id=u.get("id"), details=details, result=result)
    except Exception: pass
    try:
        c = _conn(); c.execute("INSERT INTO master_security_events(user_id,company_id,event_type,result,details,created_at) VALUES(?,?,?,?,?,?)", (u.get("id"),company_id,action,result,_json(details or {}),_now())); c.commit(); c.close()
    except Exception: pass


def _now():
    try: return _NOW()
    except Exception: return dt.datetime.now().isoformat(timespec="seconds")


def _ensure_schema():
    c = _conn()
    stmts = [
    """CREATE TABLE IF NOT EXISTS master_platform_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_tenant_meta (company_id INTEGER PRIMARY KEY, status TEXT DEFAULT 'active', notes TEXT DEFAULT '', plan_id INTEGER, deleted_at TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_feature_flags (key TEXT PRIMARY KEY, label TEXT, enabled INTEGER DEFAULT 0, description TEXT DEFAULT '', updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_company_features (company_id INTEGER, feature_key TEXT, enabled INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(company_id,feature_key))""",
    """CREATE TABLE IF NOT EXISTS master_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, price REAL DEFAULT 0, billing_cycle TEXT DEFAULT 'monthly', trial_days INTEGER DEFAULT 0, active INTEGER DEFAULT 1, limits_json TEXT DEFAULT '{}', created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, plan_id INTEGER, status TEXT DEFAULT 'trial', monthly_fee REAL DEFAULT 0, due_day INTEGER DEFAULT 10, started_at TEXT, due_at TEXT, trial_until TEXT, notes TEXT DEFAULT '', updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_permissions (key TEXT PRIMARY KEY, label TEXT, description TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS master_user_permissions (user_id INTEGER, permission_key TEXT, enabled INTEGER DEFAULT 1, updated_at TEXT, PRIMARY KEY(user_id,permission_key))""",
    """CREATE TABLE IF NOT EXISTS master_security_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_id INTEGER, event_type TEXT, result TEXT, details TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, name TEXT, key_hash TEXT UNIQUE, prefix TEXT, active INTEGER DEFAULT 1, last_used_at TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_webhooks (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, name TEXT, url TEXT, event TEXT, active INTEGER DEFAULT 0, secret TEXT, created_at TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_integrations (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, provider TEXT, status TEXT DEFAULT 'planned', config_json TEXT DEFAULT '{}', updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_maintenance_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, enabled INTEGER, message TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS master_developer_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, method TEXT, path TEXT, status_code INTEGER, duration_ms REAL, error TEXT, created_at TEXT)""",
    ]
    for s in stmts: c.execute(s)
    defaults = {
      'platform_name':'Gestão Empresarial','environment':'production','platform_version':'Enterprise','max_sessions_per_user':'10','backup_retention_days':'30','maintenance_message':'Sistema em manutenção.','session_policy':'standard'
    }
    for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO master_platform_settings(key,value,updated_at) VALUES(?,?,?)",(k,v,_now()))
    flags=[('sales','Vendas',1,'Módulo de vendas'),('stock','Estoque',1,'Módulo de estoque'),('purchases','Compras',1,'Módulo de compras'),('customers','Clientes',1,'Cadastro de clientes'),('reports','Relatórios',1,'Relatórios'),('whatsapp','WhatsApp',0,'Infraestrutura preparada; integração externa ainda não conectada'),('nfe','NF-e',0,'Infraestrutura preparada; integração fiscal não conectada'),('payments','Pagamentos',0,'Infraestrutura preparada; gateway não conectado')]
    for x in flags: c.execute("INSERT OR IGNORE INTO master_feature_flags(key,label,enabled,description,updated_at) VALUES(?,?,?,?,?)",(*x,_now()))
    perms=[('companies.read','Ver empresas'),('companies.manage','Gerenciar empresas'),('users.manage','Gerenciar usuários'),('security.manage','Gerenciar segurança'),('finance.read','Ver financeiro global'),('operations.read','Ver operações'),('backups.manage','Gerenciar backups'),('maintenance.manage','Gerenciar manutenção'),('flags.manage','Gerenciar feature flags'),('billing.manage','Gerenciar billing'),('api.manage','Gerenciar API'),('data.read','Ver Data Center')]
    for x in perms: c.execute("INSERT OR IGNORE INTO master_permissions(key,label,description) VALUES(?,?,?)",x)
    c.commit(); c.close()


def _company_count(c):
    return c.execute("SELECT COUNT(*) FROM companies").fetchone()[0] if 'companies' in _tables(c) else 0


def _sum_table(c, table, value_candidates, date_candidates=None, company_id=None):
    if table not in _tables(c): return 0.0
    val=_pick(c,table,value_candidates)
    if not val: return 0.0
    where=[]; args=[]
    cc=_pick(c,table,['company_id'])
    if company_id is not None and cc: where.append(f'"{cc}"=?'); args.append(company_id)
    q=f'SELECT COALESCE(SUM(CAST("{val}" AS REAL)),0) FROM "{table}"'+((' WHERE '+ ' AND '.join(where)) if where else '')
    try:return float(c.execute(q,args).fetchone()[0] or 0)
    except Exception:return 0.0


def _count(c,table,company_id=None):
    if table not in _tables(c): return 0
    cc=_pick(c,table,['company_id']); where=''; args=[]
    if company_id is not None and cc: where=f' WHERE "{cc}"=?'; args=[company_id]
    try:return int(c.execute(f'SELECT COUNT(*) FROM "{table}"{where}',args).fetchone()[0])
    except Exception:return 0


def _finance(c, company_id=None):
    sales=_sum_table(c,'sales',['total','total_amount','grand_total','final_total','amount','value'],company_id=company_id)
    expenses=_sum_table(c,'expenses',['amount','value','total','total_amount'],company_id=company_id)
    purchases=_sum_table(c,'purchases',['total','total_amount','amount','value'],company_id=company_id)
    return {'sales':sales,'expenses':expenses,'purchases':purchases,'result':sales-expenses,'ticket_average': sales/_count(c,'sales',company_id) if _count(c,'sales',company_id) else 0}


def _dashboard():
    c=_conn(); f=_finance(c); companies=_company_count(c)
    users=_count(c,'users'); products=_count(c,'products'); customers=_count(c,'customers'); sessions=_count(c,'sessions')
    active_users=0
    if 'users' in _tables(c):
        try: active_users=int(c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0])
        except Exception: pass
    alerts=len(_alerts(c))
    c.close()
    return {'ok':True,'finance':f,'companies':companies,'users':users,'active_users':active_users,'products':products,'customers':customers,'active_sessions':sessions,'online_users':None,'alerts':alerts,'uptime_seconds':int(time.time()-_START),'online_note':'O schema atual não possui last_activity/expiração; exibimos sessões existentes, não usuários realmente online.'}


def _alerts(c):
    a=[]
    if 'companies' in _tables(c) and 'users' in _tables(c):
        cc=_pick(c,'users',['company_id']); rc=_pick(c,'users',['role'])
        if cc and rc:
            rows=c.execute(f'''SELECT c.id,c.name FROM companies c LEFT JOIN users u ON u.company_id=c.id AND lower(u.role) IN ('admin','master') GROUP BY c.id HAVING COUNT(u.id)=0''').fetchall()
            for r in rows:a.append({'type':'company_without_admin','severity':'high','message':f'Empresa sem administrador: {r[1]}','company_id':r[0]})
    if 'products' in _tables(c):
        cols=_cols(c,'products'); stock=next((x for x in ['stock','quantity','qty','estoque'] if x in cols),None)
        if stock:
            try:
                n=c.execute(f'SELECT COUNT(*) FROM products WHERE CAST("{stock}" AS REAL)<=0').fetchone()[0]
                if n:a.append({'type':'critical_stock','severity':'medium','message':f'{n} produto(s) com estoque zerado/negativo'})
            except Exception:pass
    if 'sessions' in _tables(c):
        try:
            n=c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
            if n>50:a.append({'type':'sessions','severity':'medium','message':f'{n} sessões armazenadas'})
        except Exception:pass
    try:
        if ROOT.stat().st_size and shutil_disk_free(ROOT)<1024**3:a.append({'type':'storage','severity':'high','message':'Menos de 1 GB livre no volume do sistema'})
    except Exception:pass
    return a


def shutil_disk_free(path):
    import shutil
    return shutil.disk_usage(path).free


def _db_integrity(c):
    out=[]
    try: out.append({'check':'integrity_check','result':c.execute('PRAGMA integrity_check').fetchone()[0]})
    except Exception as e: out.append({'check':'integrity_check','result':f'erro: {e}'})
    tables=_tables(c)
    for t in ['companies','users','products','sales','expenses','purchases','customers','suppliers','sessions','audit_log']:
        out.append({'check':f'table:{t}','result':'ok' if t in tables else 'missing'})
    if 'users' in tables:
        cc=_pick(c,'users',['company_id'])
        if cc:
            try: out.append({'check':'users_without_company','result':c.execute(f'SELECT COUNT(*) FROM users WHERE "{cc}" IS NULL AND lower(role)<>"master"').fetchone()[0]})
            except Exception: pass
    if 'companies' in tables and 'users' in tables:
        try: out.append({'check':'companies_without_admin','result':len([x for x in _alerts(c) if x['type']=='company_without_admin'])})
        except Exception: pass
    return out


HTML = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Master Control</title>
<style>
:root{--bg:#080b10;--panel:#0e131b;--panel2:#111823;--line:#202a37;--text:#eef2f7;--muted:#8e9aaa;--accent:#54b8e8;--accent2:#8bd8ff;--danger:#ff647c;--warn:#f3bd5f;--ok:#58d39b;--shadow:0 18px 55px rgba(0,0,0,.28)}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#162332 0,#080b10 38%);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}.app{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{border-right:1px solid var(--line);background:rgba(7,10,14,.9);padding:22px 14px;position:sticky;top:0;height:100vh}.brand{padding:4px 10px 24px}.brand b{font-size:17px;letter-spacing:.4px}.brand span{display:block;color:var(--muted);font-size:11px;margin-top:4px}.nav button{width:100%;border:0;background:transparent;color:#aab4c1;text-align:left;padding:10px 12px;border-radius:9px;margin:2px 0;cursor:pointer}.nav button:hover,.nav button.active{background:#151d27;color:#fff}.main{padding:26px;max-width:1700px;width:100%;margin:auto}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:22px}.title h1{margin:0;font-size:27px}.title p{margin:5px 0;color:var(--muted)}.pill{padding:7px 10px;border:1px solid var(--line);border-radius:999px;color:#b9c5d2;background:#0c1118}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.card{background:linear-gradient(180deg,#111721,#0d1219);border:1px solid var(--line);border-radius:13px;padding:17px;box-shadow:var(--shadow)}.k{color:var(--muted);font-size:12px}.v{font-size:26px;font-weight:700;margin-top:8px}.sub{font-size:11px;color:var(--muted);margin-top:6px}.section{margin-top:16px}.section h2{font-size:15px;margin:0 0 10px}.panel{background:#0e131b;border:1px solid var(--line);border-radius:13px;overflow:auto}.toolbar{display:flex;gap:8px;flex-wrap:wrap;padding:12px;border-bottom:1px solid var(--line)}input,select{background:#090d13;color:#fff;border:1px solid var(--line);padding:9px 10px;border-radius:8px;outline:none}button.action{background:#17202b;color:#e8eef5;border:1px solid #2a3746;padding:9px 12px;border-radius:8px;cursor:pointer}button.action:hover{border-color:#4b6a80}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #19222e;font-size:12px}th{color:#7f8d9d;font-weight:600}td{color:#dbe2ea}.good{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--danger)}.hidden{display:none}.two{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}.bar{height:8px;background:#1b2430;border-radius:10px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}@media(max-width:1050px){.grid{grid-template-columns:repeat(2,1fr)}.app{grid-template-columns:1fr}.side{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.nav{display:flex;overflow:auto}.nav button{white-space:nowrap}.two{grid-template-columns:1fr}}@media(max-width:620px){.main{padding:14px}.grid{grid-template-columns:1fr}}
</style></head><body><div class="app"><aside class="side"><div class="brand"><b>MASTER CONTROL</b><span>Enterprise Platform Control</span></div><nav class="nav" id="nav"></nav></aside><main class="main"><div class="top"><div class="title"><h1 id="pageTitle">Command Center</h1><p id="pageDesc">Visão global da plataforma</p></div><div class="pill">MASTER · protegido</div></div><div id="content"></div></main></div>
<script>
const menus=[['dashboard','Dashboard'],['companies','Empresas'],['users','Usuários'],['operations','Operações'],['security','Segurança'],['finance','Financeiro'],['billing','Billing'],['analytics','Analytics'],['audit','Auditoria'],['alerts','Alertas'],['diagnostics','Diagnóstico'],['system','Sistema'],['backups','Backups'],['maintenance','Manutenção'],['flags','Feature Flags'],['api','API / Integrações'],['data','Data Center']];
const nav=document.querySelector('#nav'); menus.forEach(([id,label])=>{let b=document.createElement('button');b.textContent=label;b.dataset.id=id;b.onclick=()=>show(id);nav.appendChild(b)});
const $=s=>document.querySelector(s);const money=v=>Number(v||0).toLocaleString('pt-BR',{style:'currency',currency:'BRL'});const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function api(path,opt={}){let r=await fetch(path,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...opt});let j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||'Erro '+r.status);return j}
function setActive(id){document.querySelectorAll('.nav button').forEach(x=>x.classList.toggle('active',x.dataset.id===id));}
function cards(items){return '<div class="grid">'+items.map(x=>`<div class="card"><div class="k">${esc(x[0])}</div><div class="v">${esc(x[1])}</div><div class="sub">${esc(x[2]||'')}</div></div>`).join('')+'</div>'}
async function show(id){setActive(id);$('#content').innerHTML='<div class="card">Carregando dados reais...</div>';let fn=views[id]||views.dashboard;await fn();}
const views={
 dashboard:async()=>{let j=await api('/master/enterprise/api/dashboard');$('#pageTitle').textContent='Command Center';$('#pageDesc').textContent='Indicadores globais baseados no banco atual';let f=j.finance;$('#content').innerHTML=cards([['Receita',money(f.sales),'Vendas registradas'],['Despesas',money(f.expenses),'Despesas registradas'],['Resultado',money(f.result),'Receita menos despesas'],['Ticket médio',money(f.ticket_average),'Baseado nas vendas'],['Empresas',j.companies,'Tenants cadastrados'],['Usuários ativos',j.active_users,'Usuários com active=1'],['Sessões armazenadas',j.active_sessions,'O schema atual não prova presença online'],['Alertas',j.alerts,'Alertas detectados']])+`<div class="section two"><div class="panel"><div class="toolbar"><b>Saúde da plataforma</b></div><div style="padding:15px"><div class="sub">Uptime desta instância</div><div class="v">${Math.floor(j.uptime_seconds/3600)}h ${Math.floor(j.uptime_seconds%3600/60)}m</div><p class="sub">${esc(j.online_note)}</p></div></div><div class="panel"><div class="toolbar"><b>Ações rápidas</b></div><div style="padding:15px;display:flex;gap:8px;flex-wrap:wrap"><button class="action" onclick="show('diagnostics')">Executar diagnóstico</button><button class="action" onclick="show('backups')">Backup</button><button class="action" onclick="show('alerts')">Ver alertas</button></div></div></div>`},
 companies:async()=>{let j=await api('/master/enterprise/api/companies');$('#pageTitle').textContent='Empresas';$('#pageDesc').textContent='Tenants e visão operacional';$('#content').innerHTML='<div class="panel"><div class="toolbar"><input id="cs" placeholder="Pesquisar empresa..." oninput="filterTable(this.value,\'ct\')"><button class="action" onclick="loadCompanies()">Atualizar</button></div><table id="ct"><thead><tr><th>ID</th><th>Empresa</th><th>Status</th><th>Usuários</th><th>Produtos</th><th>Clientes</th><th>Vendas</th><th>Receita</th></tr></thead><tbody></tbody></table></div>';loadCompanies()},
 users:async()=>{let j=await api('/master/enterprise/api/users');$('#pageTitle').textContent='Usuários';$('#pageDesc').textContent='IAM global e controle de acesso';$('#content').innerHTML='<div class="panel"><div class="toolbar"><input placeholder="Pesquisar..." oninput="filterTable(this.value,\'ut\')"><button class="action" onclick="show(\'users\')">Atualizar</button></div><table id="ut"><thead><tr><th>ID</th><th>Nome</th><th>Usuário</th><th>Role</th><th>Empresa</th><th>Ativo</th></tr></thead><tbody>'+j.rows.map(x=>`<tr><td>${x.id}</td><td>${esc(x.name)}</td><td>${esc(x.username)}</td><td>${esc(x.role)}</td><td>${esc(x.company_name||'Global')}</td><td class="${x.active?'good':'bad'}">${x.active?'ATIVO':'BLOQUEADO'}</td></tr>`).join('')+'</tbody></table></div>'},
 operations:async()=>{let j=await api('/master/enterprise/api/operations');$('#pageTitle').textContent='Operações';$('#pageDesc').textContent='Produtos, estoque, vendas, compras, clientes e caixa';$('#content').innerHTML=cards(Object.entries(j.counts).map(([k,v])=>[k,v,'registros atuais']))},
 finance:async()=>{let j=await api('/master/enterprise/api/finance');$('#pageTitle').textContent='Financeiro Global';$('#pageDesc').textContent='Valores calculados diretamente das tabelas existentes';$('#content').innerHTML=cards([['Vendas',money(j.sales)],['Despesas',money(j.expenses)],['Compras',money(j.purchases)],['Resultado',money(j.result)],['Ticket médio',money(j.ticket_average)]])},
 security:async()=>{let j=await api('/master/enterprise/api/security');$('#pageTitle').textContent='Security Center';$('#pageDesc').textContent='Sessões, eventos e postura de segurança';$('#content').innerHTML=cards([['Sessões',j.sessions],['Usuários ativos',j.active_users],['Eventos Master',j.events],['Usuários Master',j.master_users]])+`<div class="section panel"><div class="toolbar"><b>Eventos recentes</b></div><table><thead><tr><th>Evento</th><th>Resultado</th><th>Empresa</th><th>Data</th></tr></thead><tbody>${j.recent.map(x=>`<tr><td>${esc(x.event_type)}</td><td>${esc(x.result)}</td><td>${esc(x.company_id??'—')}</td><td>${esc(x.created_at)}</td></tr>`).join('')}</tbody></table></div>`},
 alerts:async()=>{let j=await api('/master/enterprise/api/alerts');$('#pageTitle').textContent='Alert Center';$('#pageDesc').textContent='Problemas detectáveis no estado atual';$('#content').innerHTML='<div class="panel"><table><thead><tr><th>Severidade</th><th>Tipo</th><th>Mensagem</th></tr></thead><tbody>'+j.alerts.map(x=>`<tr><td class="${x.severity==='high'?'bad':x.severity==='medium'?'warn':'good'}">${esc(x.severity)}</td><td>${esc(x.type)}</td><td>${esc(x.message)}</td></tr>`).join('')+'</tbody></table></div>'},
 diagnostics:async()=>{let j=await api('/master/enterprise/api/diagnostics');$('#pageTitle').textContent='Diagnostics Center';$('#pageDesc').textContent='Integridade e isolamento multiempresa';$('#content').innerHTML='<div class="panel"><table><thead><tr><th>Teste</th><th>Resultado</th></tr></thead><tbody>'+j.checks.map(x=>`<tr><td>${esc(x.check)}</td><td class="${String(x.result)==='ok'?'good':String(x.result)==='missing'?'bad':''}">${esc(x.result)}</td></tr>`).join('')+'</tbody></table></div>'},
 backups:async()=>{let j=await api('/master/enterprise/api/backups');$('#pageTitle').textContent='Backup & Recovery';$('#pageDesc').textContent='Backups locais reais usando SQLite backup API';$('#content').innerHTML=`<div class="panel"><div class="toolbar"><button class="action" onclick="makeBackup()">Criar backup agora</button></div><table><thead><tr><th>Arquivo</th><th>Tamanho</th><th>Data</th></tr></thead><tbody>${j.rows.map(x=>`<tr><td class="mono">${esc(x.name)}</td><td>${esc(x.size)}</td><td>${esc(x.modified)}</td></tr>`).join('')}</tbody></table></div>`},
 system:async()=>{let j=await api('/master/enterprise/api/system');$('#pageTitle').textContent='Sistema';$('#pageDesc').textContent='Infraestrutura da instância';$('#content').innerHTML=cards(Object.entries(j).map(([k,v])=>[k,String(v)]))},
 data:async()=>{let j=await api('/master/enterprise/api/data');$('#pageTitle').textContent='Data Center';$('#pageDesc').textContent='Estrutura real do SQLite';$('#content').innerHTML='<div class="panel"><table><thead><tr><th>Tabela</th><th>Registros</th><th>Colunas</th><th>Índices</th></tr></thead><tbody>'+j.tables.map(x=>`<tr><td class="mono">${esc(x.name)}</td><td>${x.rows}</td><td>${x.columns}</td><td>${x.indexes}</td></tr>`).join('')+'</tbody></table></div>'},
 audit:async()=>{let j=await api('/master/enterprise/api/audit');$('#pageTitle').textContent='Audit Center';$('#pageDesc').textContent='Rastro administrativo';$('#content').innerHTML='<div class="panel"><table><thead><tr><th>Ação</th><th>Usuário</th><th>Empresa</th><th>Data</th></tr></thead><tbody>'+j.rows.map(x=>`<tr><td>${esc(x.action||x.event_type)}</td><td>${esc(x.user_id??'—')}</td><td>${esc(x.company_id??'—')}</td><td>${esc(x.created_at)}</td></tr>`).join('')+'</tbody></table></div>'},
 flags:async()=>{let j=await api('/master/enterprise/api/flags');$('#pageTitle').textContent='Feature Flags';$('#pageDesc').textContent='Controle global de módulos';$('#content').innerHTML='<div class="panel"><table><thead><tr><th>Chave</th><th>Módulo</th><th>Status</th><th>Descrição</th></tr></thead><tbody>'+j.rows.map(x=>`<tr><td class="mono">${esc(x.key)}</td><td>${esc(x.label)}</td><td class="${x.enabled?'good':'warn'}">${x.enabled?'ON':'OFF'}</td><td>${esc(x.description)}</td></tr>`).join('')+'</tbody></table></div>'},
 billing:async()=>{let j=await api('/master/enterprise/api/billing');$('#pageTitle').textContent='SaaS / Billing';$('#pageDesc').textContent='Planos e assinaturas de infraestrutura; sem gateway financeiro conectado';$('#content').innerHTML=cards([['Planos',j.plans],['Assinaturas',j.subscriptions],['MRR cadastrado',money(j.mrr)],['Em atraso',j.overdue]])+`<div class="section card"><div class="k">Observação</div><p>${esc(j.note)}</p></div>`},
 analytics:async()=>{let j=await api('/master/enterprise/api/analytics');$('#pageTitle').textContent='Analytics / BI';$('#pageDesc').textContent='Métricas de base calculadas sem inventar dados';$('#content').innerHTML=cards([['Empresas novas (30d)',j.new_companies_30d],['Empresas inativas',j.inactive_companies],['Empresas ativas',j.active_companies],['Receita global',money(j.revenue)]])},
 api:async()=>{let j=await api('/master/enterprise/api/integrations');$('#pageTitle').textContent='API / Integrações';$('#pageDesc').textContent='Chaves, webhooks e integrações cadastradas';$('#content').innerHTML=cards([['API keys',j.api_keys],['Webhooks',j.webhooks],['Integrações',j.integrations]])+`<div class="section card"><div class="k">Estado</div><p>WhatsApp, NF-e, gateways e armazenamento externo ficam como infraestrutura/configuração até haver um conector real.</p></div>`},
 maintenance:async()=>{let j=await api('/master/enterprise/api/maintenance');$('#pageTitle').textContent='Manutenção';$('#pageDesc').textContent='Controle da janela de manutenção global';$('#content').innerHTML=`<div class="card"><div class="k">Status</div><div class="v">${j.enabled?'ATIVA':'INATIVA'}</div><p>${esc(j.message||'')}</p><button class="action" onclick="toggleMaintenance(${!j.enabled})">${j.enabled?'Desativar':'Ativar'} manutenção</button></div>`}
};
async function loadCompanies(){let j=await api('/master/enterprise/api/companies');document.querySelector('#ct tbody').innerHTML=j.rows.map(x=>`<tr><td>${x.id}</td><td>${esc(x.name)}</td><td>${esc(x.status)}</td><td>${x.users}</td><td>${x.products}</td><td>${x.customers}</td><td>${x.sales_count}</td><td>${money(x.revenue)}</td></tr>`).join('')}
function filterTable(q,id){q=q.toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}
async function makeBackup(){let j=await api('/master/enterprise/api/backup',{method:'POST'});alert(j.message||j.path);show('backups')}
async function toggleMaintenance(enabled){let j=await api('/master/enterprise/api/maintenance',{method:'POST',body:JSON.stringify({enabled,message:'Manutenção administrativa do Master.'})});alert(j.message);show('maintenance')}
show('dashboard');setInterval(()=>{if(document.querySelector('[data-id="dashboard"].active'))views.dashboard()},15000);
</script></body></html>
'''

# ============================================================
# MASTER ENTERPRISE - REGISTRO DA CAMADA
# ============================================================

def register_master_enterprise(app, conn, get_user, audit, hash_password=None, now=None):

    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import os
    import platform
    import sqlite3
    import time
    import shutil
    from pathlib import Path

    _started_at = time.time()

    def master_user(request):
        try:
            user = get_user(request)
        except Exception:
            return None

        if not user:
            return None

        role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)

        if str(role).lower() != "master":
            return None

        return user

    def guard(request):
        user = master_user(request)

        if not user:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Acesso restrito ao Master"
                },
                status_code=403
            )

        return None

    def db():
        try:
            return conn()
        except Exception:
            return sqlite3.connect(str(DB_PATH))

    def table_exists(c, name):
        try:
            return bool(
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (name,)
                ).fetchone()
            )
        except Exception:
            return False

    def table_count(c, name):
        if not table_exists(c, name):
            return 0

        try:
            return int(
                c.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0] or 0
            )
        except Exception:
            return 0

    def safe_sum(c, table, columns):
        if not table_exists(c, table):
            return 0.0

        try:
            cols = set(
                x["name"]
                for x in c.execute(f'PRAGMA table_info("{table}")').fetchall()
            )

            column = next(
                (x for x in columns if x in cols),
                None
            )

            if not column:
                return 0.0

            return float(
                c.execute(
                    f'SELECT COALESCE(SUM("{column}"),0) FROM "{table}"'
                ).fetchone()[0] or 0
            )

        except Exception:
            return 0.0

    def company_rows(c):
        if not table_exists(c, "companies"):
            return []

        cols = [
            x["name"]
            for x in c.execute("PRAGMA table_info(companies)").fetchall()
        ]

        name_col = next(
            (x for x in ["name", "company_name", "razao_social"] if x in cols),
            None
        )

        status_col = next(
            (x for x in ["active", "status", "enabled"] if x in cols),
            None
        )
        segment_col = "segment" if "segment" in cols else None

        if not name_col:
            return []

        select_status = (
            f'"{status_col}" AS status'
            if status_col
            else "1 AS status"
        )
        select_segment = f'"{segment_col}" AS segment' if segment_col else "'LOJA' AS segment"

        rows = c.execute(
            f'''
            SELECT
                id,
                "{name_col}" AS name,
                {select_status},
                {select_segment}
            FROM companies
            ORDER BY id DESC
            '''
        ).fetchall()

        result = []

        for r in rows:
            cid = r["id"]

            users = 0
            products = 0
            customers = 0
            sales_count = 0
            revenue = 0.0

            try:
                if table_exists(c, "users"):
                    users = c.execute(
                        "SELECT COUNT(*) FROM users WHERE company_id=?",
                        (cid,)
                    ).fetchone()[0]
            except Exception:
                pass

            try:
                if table_exists(c, "products"):
                    products = c.execute(
                        "SELECT COUNT(*) FROM products WHERE company_id=?",
                        (cid,)
                    ).fetchone()[0]
            except Exception:
                pass

            try:
                if table_exists(c, "customers"):
                    customers = c.execute(
                        "SELECT COUNT(*) FROM customers WHERE company_id=?",
                        (cid,)
                    ).fetchone()[0]
            except Exception:
                pass

            try:
                if table_exists(c, "sales"):
                    scols = set(
                        x["name"]
                        for x in c.execute("PRAGMA table_info(sales)").fetchall()
                    )

                    if "company_id" in scols:
                        sales_count = c.execute(
                            "SELECT COUNT(*) FROM sales WHERE company_id=?",
                            (cid,)
                        ).fetchone()[0]

                        value_col = next(
                            (
                                x for x in
                                ["total", "total_amount", "amount", "valor", "value"]
                                if x in scols
                            ),
                            None
                        )

                        if value_col:
                            revenue = float(
                                c.execute(
                                    f'''
                                    SELECT COALESCE(SUM("{value_col}"),0)
                                    FROM sales
                                    WHERE company_id=?
                                    ''',
                                    (cid,)
                                ).fetchone()[0] or 0
                            )
            except Exception:
                pass

            result.append(
                {
                    "id": cid,
                    "name": r["name"],
                    "status": r["status"],
                    "segment": r["segment"] or "LOJA",
                    "users": users,
                    "products": products,
                    "customers": customers,
                    "sales_count": sales_count,
                    "revenue": revenue
                }
            )

        return result

    # --------------------------------------------------------
    # Supplemental tables
    # --------------------------------------------------------

    try:
        c = db()

        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS master_feature_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                company_id INTEGER,
                description TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                monthly_price REAL NOT NULL DEFAULT 0,
                max_users INTEGER,
                max_products INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                plan_id INTEGER,
                monthly_price REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                due_day INTEGER,
                trial_until TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                name TEXT,
                key_hash TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                url TEXT,
                event TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_integrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                provider TEXT,
                status TEXT,
                config_json TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS master_security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                company_id INTEGER,
                event_type TEXT,
                result TEXT,
                ip TEXT,
                user_agent TEXT,
                created_at TEXT
            );
            '''
        )

        defaults = [
            ("sales", "Vendas", 1, "Módulo de vendas"),
            ("stock", "Estoque", 1, "Controle de estoque"),
            ("purchases", "Compras", 1, "Módulo de compras"),
            ("customers", "Clientes", 1, "Cadastro de clientes"),
            ("reports", "Relatórios", 1, "Relatórios e indicadores"),
            ("whatsapp", "WhatsApp", 0, "Integração futura"),
            ("nfe", "NF-e", 0, "Integração fiscal futura"),
            ("payments", "Pagamentos", 0, "Gateway futuro")
        ]

        for key, label, enabled, description in defaults:
            exists = c.execute(
                "SELECT 1 FROM master_feature_flags WHERE key=? AND company_id IS NULL",
                (key,)
            ).fetchone()

            if not exists:
                c.execute(
                    '''
                    INSERT INTO master_feature_flags
                    (key,label,enabled,company_id,description,created_at)
                    VALUES (?,?,?,?,?,?)
                    ''',
                    (
                        key,
                        label,
                        enabled,
                        None,
                        description,
                        now() if now else None
                    )
                )

        c.commit()
        c.close()

    except Exception:
        pass

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    @app.get("/master/enterprise", response_class=HTMLResponse)
    async def master_enterprise_page(request: Request):
        denied = guard(request)

        if denied:
            return denied

        return HTMLResponse(HTML)

    @app.get("/master/enterprise/", response_class=HTMLResponse)
    async def master_enterprise_page_slash(request: Request):
        denied = guard(request)

        if denied:
            return denied

        return HTMLResponse(HTML)

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/dashboard")
    async def master_enterprise_dashboard(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        companies = table_count(c, "companies")
        users = table_count(c, "users")
        sessions = table_count(c, "sessions")

        active_users = 0

        if table_exists(c, "users"):
            try:
                active_users = c.execute(
                    "SELECT COUNT(*) FROM users WHERE active=1"
                ).fetchone()[0]
            except Exception:
                pass

        sales = safe_sum(
            c,
            "sales",
            ["total", "total_amount", "amount", "valor", "value"]
        )

        expenses = safe_sum(
            c,
            "expenses",
            ["amount", "valor", "value", "total"]
        )

        sales_count = table_count(c, "sales")

        ticket = sales / sales_count if sales_count else 0

        alerts = 0

        try:
            alerts = len(_alerts(c))
        except Exception:
            pass

        c.close()

        return {
            "ok": True,
            "finance": {
                "sales": sales,
                "expenses": expenses,
                "result": sales - expenses,
                "ticket_average": ticket
            },
            "companies": companies,
            "users": users,
            "active_users": active_users,
            "active_sessions": sessions,
            "alerts": alerts,
            "uptime_seconds": time.time() - _started_at,
            "online_note": "Sessões armazenadas não representam necessariamente usuários online."
        }

    # --------------------------------------------------------
    # COMPANIES
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/companies")
    async def master_enterprise_companies(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()
        rows = company_rows(c)
        c.close()

        return {
            "ok": True,
            "rows": rows
        }

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/users")
    async def master_enterprise_users(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        if not table_exists(c, "users"):
            c.close()
            return {"ok": True, "rows": []}

        rows = c.execute(
            '''
            SELECT
                u.id,
                u.name,
                u.username,
                u.role,
                u.active,
                u.company_id,
                c.name AS company_name
            FROM users u
            LEFT JOIN companies c ON c.id=u.company_id
            ORDER BY u.id DESC
            '''
        ).fetchall()

        result = [dict(x) for x in rows]

        c.close()

        return {
            "ok": True,
            "rows": result
        }

    # --------------------------------------------------------
    # OPERATIONS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/operations")
    async def master_enterprise_operations(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        tables = [
            "products",
            "stock_movements",
            "sales",
            "sale_items",
            "purchases",
            "purchase_items",
            "customers",
            "suppliers",
            "cash_sessions",
            "cash_movements",
            "expenses"
        ]

        counts = {
            t: table_count(c, t)
            for t in tables
        }

        c.close()

        return {
            "ok": True,
            "counts": counts
        }

    # --------------------------------------------------------
    # FINANCE
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/finance")
    async def master_enterprise_finance(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        sales = safe_sum(
            c,
            "sales",
            ["total", "total_amount", "amount", "valor", "value"]
        )

        expenses = safe_sum(
            c,
            "expenses",
            ["amount", "valor", "value", "total"]
        )

        purchases = safe_sum(
            c,
            "purchases",
            ["total", "total_amount", "amount", "valor", "value"]
        )

        sales_count = table_count(c, "sales")

        c.close()

        return {
            "ok": True,
            "sales": sales,
            "expenses": expenses,
            "purchases": purchases,
            "result": sales - expenses,
            "ticket_average": sales / sales_count if sales_count else 0
        }

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/security")
    async def master_enterprise_security(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        sessions = table_count(c, "sessions")
        active_users = 0
        master_users = 0
        events = table_count(c, "master_security_events")

        if table_exists(c, "users"):
            try:
                active_users = c.execute(
                    "SELECT COUNT(*) FROM users WHERE active=1"
                ).fetchone()[0]

                master_users = c.execute(
                    "SELECT COUNT(*) FROM users WHERE lower(role)='master'"
                ).fetchone()[0]
            except Exception:
                pass

        recent = []

        if table_exists(c, "master_security_events"):
            try:
                recent = [
                    dict(x)
                    for x in c.execute(
                        '''
                        SELECT
                            event_type,
                            result,
                            company_id,
                            created_at
                        FROM master_security_events
                        ORDER BY id DESC
                        LIMIT 30
                        '''
                    ).fetchall()
                ]
            except Exception:
                pass

        c.close()

        return {
            "ok": True,
            "sessions": sessions,
            "active_users": active_users,
            "master_users": master_users,
            "events": events,
            "recent": recent
        }

    # --------------------------------------------------------
    # ALERTS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/alerts")
    async def master_enterprise_alerts(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        try:
            alerts = _alerts(c)
        except Exception as e:
            alerts = [
                {
                    "type": "system",
                    "severity": "high",
                    "message": str(e)
                }
            ]

        c.close()

        return {
            "ok": True,
            "alerts": alerts
        }

    # --------------------------------------------------------
    # DIAGNOSTICS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/diagnostics")
    async def master_enterprise_diagnostics(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        try:
            checks = _db_integrity(c)
        except Exception as e:
            checks = [
                {
                    "check": "diagnostics",
                    "result": f"erro: {e}"
                }
            ]

        c.close()

        return {
            "ok": True,
            "checks": checks
        }

    # --------------------------------------------------------
    # BACKUPS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/backups")
    async def master_enterprise_backups(request: Request):

        denied = guard(request)
        if denied:
            return denied

        folder = Path(DB_PATH).parent / "BACKUPS"

        folder.mkdir(
            parents=True,
            exist_ok=True
        )

        rows = []

        for file in sorted(
            folder.glob("*"),
            key=lambda x: x.stat().st_mtime if x.exists() else 0,
            reverse=True
        )[:100]:

            if not file.is_file():
                continue

            try:
                size = file.stat().st_size
                modified = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(file.stat().st_mtime)
                )

                rows.append(
                    {
                        "name": file.name,
                        "size": size,
                        "modified": modified
                    }
                )
            except Exception:
                pass

        return {
            "ok": True,
            "rows": rows
        }

    @app.post("/master/enterprise/api/backup")
    async def master_enterprise_backup(request: Request):

        denied = guard(request)
        if denied:
            return denied

        source = Path(DB_PATH)
        folder = source.parent / "BACKUPS"

        folder.mkdir(
            parents=True,
            exist_ok=True
        )

        stamp = time.strftime("%Y%m%d_%H%M%S")

        destination = folder / f"master_backup_{stamp}.db"

        try:
            source_conn = sqlite3.connect(str(source))
            destination_conn = sqlite3.connect(str(destination))

            with destination_conn:
                source_conn.backup(destination_conn)

            destination_conn.close()
            source_conn.close()

            try:
                audit(
                    request,
                    "master_backup",
                    None,
                    f"Backup criado: {destination.name}"
                )
            except Exception:
                pass

            return {
                "ok": True,
                "message": f"Backup criado com sucesso: {destination.name}",
                "path": str(destination)
            }

        except Exception as e:

            return JSONResponse(
                {
                    "ok": False,
                    "error": str(e)
                },
                status_code=500
            )

    # --------------------------------------------------------
    # SYSTEM
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/system")
    async def master_enterprise_system(request: Request):

        denied = guard(request)
        if denied:
            return denied

        try:
            disk = shutil.disk_usage(Path(DB_PATH).parent)

            storage_free = disk.free
            storage_total = disk.total
        except Exception:
            storage_free = 0
            storage_total = 0

        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "system": platform.system(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - _started_at, 2),
            "database": str(DB_PATH),
            "database_size": Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0,
            "storage_total": storage_total,
            "storage_free": storage_free
        }

    # --------------------------------------------------------
    # DATA CENTER
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/data")
    async def master_enterprise_data(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        tables = []

        names = [
            x["name"]
            for x in c.execute(
                '''
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                '''
            ).fetchall()
        ]

        for name in names:

            columns = 0
            indexes = 0
            rows = 0

            try:
                columns = len(
                    c.execute(
                        f'PRAGMA table_info("{name}")'
                    ).fetchall()
                )
            except Exception:
                pass

            try:
                indexes = len(
                    c.execute(
                        f'PRAGMA index_list("{name}")'
                    ).fetchall()
                )
            except Exception:
                pass

            try:
                rows = c.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
            except Exception:
                pass

            tables.append(
                {
                    "name": name,
                    "rows": rows,
                    "columns": columns,
                    "indexes": indexes
                }
            )

        c.close()

        return {
            "ok": True,
            "tables": tables
        }

    # --------------------------------------------------------
    # AUDIT
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/audit")
    async def master_enterprise_audit(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        rows = []

        if table_exists(c, "audit_log"):

            try:
                rows = [
                    dict(x)
                    for x in c.execute(
                        '''
                        SELECT *
                        FROM audit_log
                        ORDER BY rowid DESC
                        LIMIT 200
                        '''
                    ).fetchall()
                ]
            except Exception:
                pass

        c.close()

        return {
            "ok": True,
            "rows": rows
        }

    # --------------------------------------------------------
    # FEATURE FLAGS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/flags")
    async def master_enterprise_flags(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        rows = [
            dict(x)
            for x in c.execute(
                '''
                SELECT
                    key,
                    label,
                    enabled,
                    description
                FROM master_feature_flags
                WHERE company_id IS NULL
                ORDER BY id
                '''
            ).fetchall()
        ]

        c.close()

        return {
            "ok": True,
            "rows": rows
        }

    # --------------------------------------------------------
    # BILLING
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/billing")
    async def master_enterprise_billing(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        plans = table_count(c, "master_plans")
        subscriptions = table_count(c, "master_subscriptions")

        mrr = safe_sum(
            c,
            "master_subscriptions",
            ["monthly_price"]
        )

        overdue = 0

        if table_exists(c, "master_subscriptions"):

            try:
                overdue = c.execute(
                    '''
                    SELECT COUNT(*)
                    FROM master_subscriptions
                    WHERE lower(status) IN ('overdue','atrasado','past_due')
                    '''
                ).fetchone()[0]
            except Exception:
                pass

        c.close()

        return {
            "ok": True,
            "plans": plans,
            "subscriptions": subscriptions,
            "mrr": mrr,
            "overdue": overdue,
            "note": "Billing estrutural. Nenhum gateway financeiro externo está conectado."
        }

    # --------------------------------------------------------
    # ANALYTICS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/analytics")
    async def master_enterprise_analytics(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        active = 0
        inactive = 0

        if table_exists(c, "companies"):

            cols = set(
                x["name"]
                for x in c.execute("PRAGMA table_info(companies)").fetchall()
            )

            status_col = next(
                (
                    x for x in
                    ["active", "enabled"]
                    if x in cols
                ),
                None
            )

            if status_col:

                try:
                    active = c.execute(
                        f'SELECT COUNT(*) FROM companies WHERE "{status_col}"=1'
                    ).fetchone()[0]

                    inactive = c.execute(
                        f'SELECT COUNT(*) FROM companies WHERE "{status_col}"=0'
                    ).fetchone()[0]

                except Exception:
                    pass

        revenue = safe_sum(
            c,
            "sales",
            ["total", "total_amount", "amount", "valor", "value"]
        )

        c.close()

        return {
            "ok": True,
            "new_companies_30d": 0,
            "inactive_companies": inactive,
            "active_companies": active,
            "revenue": revenue
        }

    # --------------------------------------------------------
    # API / INTEGRATIONS
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/integrations")
    async def master_enterprise_integrations(request: Request):

        denied = guard(request)
        if denied:
            return denied

        c = db()

        result = {
            "api_keys": table_count(c, "master_api_keys"),
            "webhooks": table_count(c, "master_webhooks"),
            "integrations": table_count(c, "master_integrations")
        }

        c.close()

        return {
            "ok": True,
            **result
        }

    # --------------------------------------------------------
    # MAINTENANCE
    # --------------------------------------------------------

    @app.get("/master/enterprise/api/maintenance")
    async def master_enterprise_maintenance_get(request: Request):

        denied = guard(request)
        if denied:
            return denied

        try:
            if "MASTER_MAINTENANCE" in globals():
                state = MASTER_MAINTENANCE
            else:
                state = {
                    "enabled": False,
                    "message": ""
                }

            return {
                "ok": True,
                "enabled": bool(state.get("enabled")),
                "message": state.get("message", "")
            }

        except Exception:
            return {
                "ok": True,
                "enabled": False,
                "message": ""
            }

    @app.post("/master/enterprise/api/maintenance")
    async def master_enterprise_maintenance_post(request: Request):

        denied = guard(request)
        if denied:
            return denied

        global MASTER_MAINTENANCE

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        enabled = bool(payload.get("enabled"))
        message = str(
            payload.get(
                "message",
                "Manutenção administrativa do Master."
            )
        )

        MASTER_MAINTENANCE = {
            "enabled": enabled,
            "message": message
        }

        try:
            if audit:
                audit(
                    request,
                    "master_maintenance",
                    None,
                    f"enabled={enabled}"
                )
        except Exception:
            pass

        return {
            "ok": True,
            "enabled": enabled,
            "message": message
        }

    print("[MASTER ENTERPRISE] camada registrada com sucesso.")
    return True
