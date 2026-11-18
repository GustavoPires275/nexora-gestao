(() => {
  'use strict';
  const token = decodeURIComponent(location.pathname.split('/').filter(Boolean).at(-1) || '');
  const clientStorage = 'restaurant_menu_client_v1';
  const clientId = localStorage.getItem(clientStorage) || (() => {
    const value = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(24)), x => x.toString(16).padStart(2, '0')).join('');
    localStorage.setItem(clientStorage, value);
    return value;
  })();
  const cart = new Map();
  const state = { data: null, category: 'Todos', requestId: null };
  const money = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
  const el = id => document.getElementById(id);

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, cache: 'no-store' });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.message || `Falha ao carregar (${response.status})`);
    return body;
  }

  function notice(message, error = false) {
    const node = el('notice'); node.textContent = message; node.hidden = false; node.classList.toggle('error', error);
    window.clearTimeout(notice.timer); notice.timer = window.setTimeout(() => { node.hidden = true; }, 5000);
  }

  function renderCategories() {
    const categories = ['Todos', ...state.data.categories];
    const nav = el('categories'); nav.replaceChildren();
    for (const category of categories) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = category;
      button.classList.toggle('active', category === state.category);
      button.addEventListener('click', () => { state.category = category; renderCategories(); renderProducts(); });
      nav.append(button);
    }
  }

  function renderProducts() {
    const menu = el('menu'); menu.replaceChildren();
    const products = state.data.products.filter(p => state.category === 'Todos' || (p.category || 'Outros') === state.category);
    if (!products.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'Nenhum produto nesta categoria.'; menu.append(empty); return; }
    for (const product of products) {
      const card = document.createElement('article'); card.className = `product${product.image_url ? ' has-image' : ''}`;
      const info = document.createElement('div');
      const title = document.createElement('h2'); title.textContent = product.name; info.append(title);
      if (product.description) { const description = document.createElement('p'); description.textContent = product.description; info.append(description); }
      const price = document.createElement('div'); price.className = 'price'; price.textContent = money.format(Number(product.price)); info.append(price); card.append(info);
      const side = document.createElement('div'); side.className = 'product-side';
      if (product.available) {
        const add = document.createElement('button'); add.type = 'button'; add.className = 'add'; add.textContent = cart.has(product.id) ? 'Adicionar +' : 'Adicionar';
        add.addEventListener('click', () => addProduct(product)); side.append(add);
      } else { const unavailable = document.createElement('span'); unavailable.className = 'unavailable'; unavailable.textContent = 'Indisponível'; side.append(unavailable); }
      card.append(side);
      if (product.image_url) { const image = document.createElement('img'); image.src = product.image_url; image.alt = product.name; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer'; card.append(image); }
      menu.append(card);
    }
  }

  function addProduct(product) {
    const line = cart.get(product.id) || { product, qty: 0, notes: '' }; line.qty += 1; cart.set(product.id, line); renderProducts(); renderCartCount();
  }

  function renderCartCount() { el('cart-count').textContent = String([...cart.values()].reduce((sum, line) => sum + line.qty, 0)); }

  function renderCart() {
    const root = el('cart-lines'); root.replaceChildren(); let subtotal = 0;
    for (const [id, line] of cart) {
      subtotal += Number(line.product.price) * line.qty;
      const row = document.createElement('div'); row.className = 'cart-line';
      const name = document.createElement('strong'); name.textContent = line.product.name;
      const qty = document.createElement('div'); qty.className = 'qty';
      const minus = document.createElement('button'); minus.type = 'button'; minus.textContent = '−'; minus.setAttribute('aria-label', 'Diminuir quantidade');
      minus.addEventListener('click', () => { line.qty -= 1; if (line.qty <= 0) cart.delete(id); renderCart(); renderCartCount(); });
      const count = document.createElement('span'); count.textContent = String(line.qty);
      const plus = document.createElement('button'); plus.type = 'button'; plus.textContent = '+'; plus.setAttribute('aria-label', 'Aumentar quantidade');
      plus.addEventListener('click', () => { line.qty += 1; renderCart(); renderCartCount(); }); qty.append(minus, count, plus);
      const noteLabel = document.createElement('label'); noteLabel.textContent = 'Observação do preparo';
      const note = document.createElement('input'); note.maxLength = 500; note.placeholder = 'Ex.: sem cebola'; note.value = line.notes;
      note.addEventListener('input', () => { line.notes = note.value; });
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'remove'; remove.textContent = 'Remover';
      remove.addEventListener('click', () => { cart.delete(id); renderCart(); renderCartCount(); renderProducts(); });
      row.append(name, qty, noteLabel, note, remove); root.append(row);
    }
    el('cart-total').textContent = money.format(subtotal);
    el('submit-order').disabled = cart.size === 0;
  }

  async function submitOrder() {
    if (!cart.size) return;
    const button = el('submit-order'); button.disabled = true; button.textContent = 'Enviando…';
    state.requestId ||= crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const result = await api(`/api/public/tables/${encodeURIComponent(token)}/orders`, { method: 'POST', body: JSON.stringify({ client_id: clientId, request_id: state.requestId, items: [...cart.values()].map(line => ({ product_id: line.product.id, qty: line.qty, notes: line.notes })) }) });
      cart.clear(); state.requestId = null; el('cart-dialog').close(); renderCartCount(); renderProducts();
      notice(`Pedido #${result.order_id} enviado para a cozinha. Você pode pedir mais itens.`);
    } catch (error) { notice(error.message, true); }
    finally { button.textContent = 'Enviar para a cozinha'; renderCart(); }
  }

  async function request(kind) {
    const button = el(kind === 'WAITER' ? 'waiter' : 'bill'); button.disabled = true;
    try { const result = await api(`/api/public/tables/${encodeURIComponent(token)}/requests`, { method: 'POST', body: JSON.stringify({ client_id: clientId, kind }) }); notice(result.message); }
    catch (error) { notice(error.message, true); }
    finally { window.setTimeout(() => { button.disabled = false; }, 30000); }
  }

  async function start() {
    if (!token) throw new Error('QR Code inválido. Peça ajuda à equipe.');
    el('menu').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    state.data = await api(`/api/public/menu/${encodeURIComponent(token)}`);
    document.title = `${state.data.restaurant_name} · Cardápio`;
    el('restaurant').textContent = state.data.restaurant_name; el('table-label').textContent = state.data.table_name;
    renderCategories(); renderProducts();
  }

  el('review').addEventListener('click', () => { renderCart(); el('cart-dialog').showModal(); });
  el('submit-order').addEventListener('click', submitOrder);
  el('waiter').addEventListener('click', () => request('WAITER'));
  el('bill').addEventListener('click', () => request('BILL'));
  start().catch(error => { el('restaurant').textContent = 'Cardápio indisponível'; el('table-label').textContent = ''; el('menu').replaceChildren(); notice(error.message, true); });
})();
