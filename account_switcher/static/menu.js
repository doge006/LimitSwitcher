'use strict';
// Menu bar popover (macOS). Same local API as the full view; the native host listens for
// {type: 'height' | 'width' | 'full' | 'dock' | 'quit'} messages and resizes / opens windows accordingly.
// Dragged off the menu bar, the popover detaches and stays open; the host then calls
// setDetached(true): the page shows a button that docks it again.
const hash = new URLSearchParams(location.hash.slice(1));
const token = hash.get('token');
let detached = false;
const native = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.app;
const $ = id => document.getElementById(id);
const PROVIDERS = [['claude', 'Claude'], ['codex', 'Codex']];
let state = null, pending = null, armed = null, armedTimer = null;

const remaining = used => Math.max(0, Math.min(100, Math.floor(100 - used + 1e-6)));  // rounded down
const level = left => left > 30 ? 'good' : left > 10 ? 'warn' : 'bad';
function until(ts) {
  const m = Math.max(0, Math.round((ts * 1000 - Date.now()) / 60000));
  if (m < 60) return `${m}m`;
  if (m < 1440) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return `${Math.floor(m / 1440)}d ${Math.floor(m % 1440 / 60)}h`;
}
const short = w => ({ five_hour: '5h', weekly: '1w', monthly: '30d' })[w.key] || w.label.replace('Weekly · ', '');
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function post(message) { if (native) native.postMessage(message); }
if (native) document.documentElement.classList.add('native');

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  return response.json();
}
const act = (action, body) => api(`/api/${action}`, body).catch(() => {});

function row(account) {
  const relogin = /sign in|expired|missing/i.test(account.status || '');
  const switchable = account.eligible && !account.active && !pending && !state.busy && !relogin;  // signed in again first
  const confirming = switchable && armed === account.id;
  const r = el('div', 'row' + (account.active ? ' active' : '') + (account.eligible ? '' : ' spent')
    + (switchable ? ' switchable' : '') + (confirming ? ' confirm' : ''));
  const head = el('div', 'head');
  head.append(el('span', 'email', account.name));
  if (account.plan) head.append(el('span', `plan ${account.provider}`, account.plan));
  if (account.updated_at) {  // how old the numbers are, on every account (amber once 30 minutes old)
    const age = Math.floor((Date.now() / 1000 - account.updated_at) / 60);
    head.append(el('span', age >= 30 ? 'age old' : 'age', age < 1 ? 'now' : age < 60 ? `${age}m ago` : `${Math.floor(age / 60)}h ago`));
  }
  const windows = account.windows.slice(0, 3);
  if (pending === account.id) head.append(el('span', 'state', 'Switching…'));
  else if (confirming) head.append(el('span', 'state confirm', 'Click again'));
  else if (relogin) {  // the text itself is the button: the app's own sign-in, other logins untouched
    const again = el('button', 'state relogin', 'Login expired · Sign in again');
    again.type = 'button';
    again.title = `Opens the sign-in: sign in as ${account.name}`;
    again.addEventListener('click', event => { event.stopPropagation(); act('add', { provider: account.provider, id: account.id }); });
    head.append(again);
  }
  else if (account.active) head.append(el('span', 'state in-use', 'In use'));
  else if (!account.eligible) head.append(el('span', 'state limit', 'Limit'));
  else if (account.status && windows.length) head.append(el('span', 'state note', account.status));
  else if (switchable) head.append(el('span', 'state hint', 'Switch'));
  r.append(head);
  if (switchable) {
    // Two clicks, like the Windows panel: the first asks for confirmation, so a stray click never switches.
    r.addEventListener('click', () => {
      clearTimeout(armedTimer);
      if (!confirming) {
        armed = account.id;
        armedTimer = setTimeout(() => { armed = null; render(); }, 4000);
        render();
        return;
      }
      armed = null;
      pending = account.id;
      render();
      act('swap', { id: account.id });
    });
  }
  if (windows.length) {
    const bars = el('div', 'bars');
    bars.style.setProperty('--cols', windows.length);
    for (const w of windows) {
      const left = remaining(w.used), meter = el('div', 'meter');
      const track = el('span', 'track'), fill = el('span', `fill ${level(left)}`);
      fill.style.width = `${left}%`;
      track.append(fill);
      meter.append(el('span', 'label', short(w)), track, el('span', `pct ${level(left)}-text`, `${Math.round(left)}%`));
      const reset = el('span', 'reset' + (windows.length > 2 ? ' compact' : ''),
        w.resetsAt ? (windows.length > 2 ? until(w.resetsAt) : `resets in ${until(w.resetsAt)}`) : '');
      if (w.resetsAt) reset.title = `resets in ${until(w.resetsAt)}`;
      meter.append(reset, el('span', 'left', 'left'));
      bars.append(meter);
    }
    r.append(bars);
  } else {
    r.append(el('div', 'loading', relogin ? 'Usage paused until you sign in again' : account.status || 'Usage not loaded yet'));
  }
  return r;
}

const COMPACT = 'M2.5 6H6V2.5M13.5 6H10V2.5M2.5 10H6v3.5M13.5 10H10v3.5';  // corners pointing in
const EXPAND = 'M2.5 6V2.5H6M13.5 6V2.5H10M2.5 10v3.5H6M13.5 10v3.5H10';   // corners pointing out

const DOCK = ['M2.5 2.5h11', 'M8 13.5V5.5M5 8.5l3-3 3 3'];  // an arrow up to the menu bar
const QUIT = ['M5.2 3.8a5.5 5.5 0 1 0 5.6 0', 'M8 1.6v6'];

function toolButton(paths, title, onClick) {
  const b = el('button', 'icon-btn tiny');
  b.type = 'button';
  b.title = title;
  b.setAttribute('aria-label', title);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  for (const d of [].concat(paths)) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  b.append(svg);
  b.addEventListener('click', event => { event.stopPropagation(); onClick(); });
  return b;
}

function compactTools() {
  // Compact: its buttons sit in line with the first account, top right (no header or footer).
  const tools = el('span', 'tools');
  if (detached) tools.append(toolButton(DOCK, 'Back to the menu bar', dock));
  tools.append(toolButton(EXPAND, 'Show everything', toggleSize), toolButton(QUIT, 'Quit LimitSwitcher', quit));
  return tools;
}

function compactRow(account, first) {
  // Compact panel: the account in use, its meters and what's left; nothing to switch.
  const r = el('div', 'row mini');
  const head = el('div', 'head');
  const icon = el('img', 'provider-icon');
  icon.src = `/assets/${account.provider}.png`;
  icon.alt = '';
  head.append(icon, el('span', 'email', account.name));
  const full = row(account);
  const relogin = full.querySelector('.state.relogin');
  if (relogin) head.append(relogin);
  if (first) head.append(compactTools());
  r.append(head);
  const bars = full.querySelector('.bars') || full.querySelector('.loading');
  if (bars) r.append(bars);
  return r;
}

const slots = new Map();  // compact: provider -> {slot, row, id}, kept between renders so things can move

function renderCompact(list) {
  if (list.dataset.mode !== 'compact') {
    list.replaceChildren();
    list.dataset.mode = 'compact';
    slots.clear();
  }
  const inUse = PROVIDERS.map(([id]) => state.accounts.find(a => a.provider === id && a.active)).filter(Boolean);
  list.querySelector('.none')?.remove();
  for (const [provider, entry] of [...slots]) {
    if (!inUse.some(a => a.provider === provider)) { entry.slot.remove(); slots.delete(provider); }
  }
  inUse.forEach((account, index) => {
    let entry = slots.get(account.provider);
    if (!entry) {
      entry = { slot: el('div', 'slot'), row: null, id: null };
      slots.set(account.provider, entry);
    }
    list.append(entry.slot);  // keeps provider order
    const next = compactRow(account, index === 0);
    if (entry.row && entry.id !== account.id) {
      // Swapped: the old account slides out to the left, then the new one slides in from the right.
      const old = entry.row;
      old.classList.add('leaving');
      old.addEventListener('animationend', () => old.remove(), { once: true });
      next.classList.add('entering');
      entry.slot.append(next);
    } else if (entry.row) {
      // Same account: the meters glide from their old width to the new one.
      const before = [...entry.row.querySelectorAll('.fill')].map(f => f.style.width);
      const fills = [...next.querySelectorAll('.fill')];
      const targets = fills.map(f => f.style.width);
      fills.forEach((f, i) => { if (before[i]) f.style.width = before[i]; });
      entry.slot.replaceChildren(next);
      requestAnimationFrame(() => requestAnimationFrame(() => fills.forEach((f, i) => { f.style.width = targets[i]; })));
    } else {
      entry.slot.append(next);
    }
    entry.row = next;
    entry.id = account.id;
  });
  if (!inUse.length) {
    const none = el('div', 'row mini none');
    const head = el('div', 'head');
    head.append(el('span', 'email muted', 'No account in use'), compactTools());
    none.append(head);
    list.append(none);
  }
}

// A large session waiting for an OK before it continues on the new account (loading it costs usage).
function addAsks(list) {
  for (const item of (state.pendingResumes || []).slice().reverse()) {
    const ask = el('div', 'ask');
    ask.append(el('b', '', `Large session (~${Math.round(item.tokens / 1000)}k tokens)`),
      document.createTextNode('Continuing loads all of it on the new account, which can use a lot of usage.'));
    const yes = el('button', 'pill', 'Continue');
    const no = el('button', 'pill', "Don't");
    yes.type = no.type = 'button';
    yes.addEventListener('click', () => act('resumeSession', { session: item.session, approve: true }));
    no.addEventListener('click', () => act('resumeSession', { session: item.session, approve: false }));
    ask.append(yes, no);
    list.prepend(ask);
  }
}

function render() {
  const list = $('list');
  const compact = !!state.compact;
  document.body.classList.toggle('compact', compact);
  $('size-icon').setAttribute('d', compact ? EXPAND : COMPACT);
  $('size').title = compact ? 'Show everything' : 'Compact: only the accounts in use';
  $('dock').hidden = !detached;
  post({ type: 'width', value: compact ? 320 : 392 });
  if (compact) {
    renderCompact(list);
    addAsks(list);
    return;
  }
  list.dataset.mode = 'full';
  slots.clear();
  list.replaceChildren();
  for (const [id, name] of PROVIDERS) {
    const accounts = state.accounts.filter(a => a.provider === id);
    if (!accounts.length) continue;
    const section = el('div', `section ${id}`);
    const icon = el('img');
    icon.src = `/assets/${id}.png`;
    icon.alt = '';
    section.append(icon, document.createTextNode(name.toUpperCase()), el('span', 'count', String(accounts.length)));
    list.append(section, ...accounts.map(row));
  }
  if (!state.accounts.length) {
    const empty = el('div', 'empty');
    empty.append(el('b', '', 'No accounts yet'), document.createTextNode('Sign in to Claude Code or Codex and it shows up here.'));
    list.append(empty);
  }
  addAsks(list);
  $('auto').checked = state.autoSwap;
  $('afk').checked = state.afk;
  $('auto').disabled = $('afk').disabled = !!state.busy;
}

// The panel follows the page's height whenever it changes (after rendering, once images and
// fonts have loaded, when a row animates in), so it never opens cut short.
let postedHeight = 0;
new ResizeObserver(() => {
  const height = Math.ceil(document.body.getBoundingClientRect().height);
  if (height && height !== postedHeight) { postedHeight = height; post({ type: 'height', value: height }); }
}).observe(document.body);
window.setDetached = on => { detached = !!on; if (state) render(); };
// Dragging the panel by its header or background: the host moves the window with the mouse.
// Dragged away from the menu bar it stays open (detached); the dock button slides it back.
document.addEventListener('mousedown', event => {
  if (!native || event.button !== 0) return;
  if (event.target.closest('button, input, label, a, select, .row:not(.mini)')) return;  // controls and accounts stay clickable
  event.preventDefault();  // no text selection while dragging
  post({ type: 'dragStart' });
  let queued = false;
  const move = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; post({ type: 'dragMove' }); });
  };
  const end = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', end);
    post({ type: 'dragEnd' });
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', end);
});
document.addEventListener('keydown', event => { if (event.key === 'Escape') post({ type: 'escape' }); });

async function follow() {
  let revision = -1;
  for (;;) {
    try {
      state = await api(`/api/state?after=${revision}`);
      revision = state.revision;
      if (pending && state.accounts.some(a => a.id === pending && a.active)) pending = null;
      render();
    } catch {
      await new Promise(done => setTimeout(done, 1500));
    }
  }
}

for (const id of ['auto', 'afk']) {
  $(id).addEventListener('change', () => act('preferences', { autoSwap: $('auto').checked, afk: $('afk').checked }));
}
$('full').addEventListener('click', () => native ? post({ type: 'full' }) : window.open(`/#token=${token}`));
function toggleSize() { if (state) { state.compact = !state.compact; render(); act('compact', { on: state.compact }); } }
function dock() { post({ type: 'dock' }); }
function quit() { native ? post({ type: 'quit' }) : act('shutdown'); }
$('size').addEventListener('click', toggleSize);
$('dock').addEventListener('click', dock);
$('quit').addEventListener('click', () => native ? post({ type: 'quit' }) : act('shutdown'));
setInterval(() => state && render(), 60000);  // keep "resets in" current
follow();
