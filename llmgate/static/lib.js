// DOM builder, API client and small helpers. Text is only ever set via textContent (no innerHTML).
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'value') el.value = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  // Tags are coloured by their text, so the same label has the same colour everywhere.
  if (tag === 'span' && el.classList.contains('tag')) el.style.setProperty('--tc', `var(--c${slot(el.textContent)})`);
  return el;
}

/** Stable 1..8 palette slot for a name: an entity keeps its colour across charts, tags and tables. */
export function slot(text) {
  let n = 0;
  for (const ch of String(text)) n = (n * 31 + ch.codePointAt(0)) >>> 0;
  return (n % 8) + 1;
}

const ICONS = {
  overview: 'M3 3h7v9H3z M14 3h7v5h-7z M14 12h7v9h-7z M3 16h7v5H3z',
  logs: 'M4 6h16 M4 12h16 M4 18h10',
  providers: 'M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01',
  models: 'M12 3l9 5-9 5-9-5z M3 13l9 5 9-5 M3 17.5l9 5 9-5',
  services: 'M15 7a4 4 0 1 1-4 4 M11 11L3 19 M7 15l3 3 M5 17l2 2',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M22 21v-2a4 4 0 0 0-3-3.9 M16 3.1a4 4 0 0 1 0 7.8',
  controls: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z M9 12l2 2 4-4',
  playground: 'M6 4l14 8-14 8z',
  requests: 'M13 2L4 14h7l-1 8 9-12h-7z',
  tokens: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v10 M9.5 9.5h4a1.8 1.8 0 0 1 0 3.5h-3a1.8 1.8 0 0 0 0 3.5h4',
  cost: 'M12 2v20 M17 6.5H9.5a3 3 0 0 0 0 6h5a3 3 0 0 1 0 6H6',
  latency: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2',
  cache: 'M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  health: 'M3 12h4l3-7 4 14 3-7h4',
};

/** Small stroke icon (inherits currentColor). */
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(k, v);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name] || ICONS.overview);
  svg.append(path);
  return svg;
}

export const fmt = {
  int: (n) => (n ?? 0).toLocaleString('ko-KR'),
  compact: (n) => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n ?? 0),
  usd: (n) => '$' + (n ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: n && n < 0.01 ? 6 : 2 }),
  ms: (n) => (n == null ? '–' : n >= 1000 ? (n / 1000).toFixed(2) + ' s' : Math.round(n) + ' ms'),
  pct: (n) => (n * 100).toFixed(1) + '%',
  time: (ts) => (ts ? new Date(ts * 1000).toLocaleString('ko-KR', { hour12: false }) : '–'),
  ago: (ts) => {
    if (!ts) return '–';
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60 ? `${Math.floor(s)}초 전` : s < 3600 ? `${Math.floor(s / 60)}분 전` : s < 86400 ? `${Math.floor(s / 3600)}시간 전` : `${Math.floor(s / 86400)}일 전`;
  },
};

const TOKEN_KEY = 'llmgate_admin_token';
export const auth = {
  get: () => sessionStorage.getItem(TOKEN_KEY) || '',
  set: (t) => sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};
// The signed-in principal ({email, role, kind, permissions}); set by app.js after /auth/me.
export const me = { value: null };
export const can = (role) => !!me.value && me.value.permissions.includes(role);

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/** Calls /admin/api (or any admin-guarded path). Uses the SSO session cookie, or a bearer token if one was entered. */
export async function api(method, path, body, { root = '/admin/api' } = {}) {
  const token = auth.get();
  const res = await fetch(root + path, {
    method,
    credentials: 'same-origin',
    headers: { 'x-llmgate-csrf': '1', ...(token ? { Authorization: 'Bearer ' + token } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error?.message || (Array.isArray(data.detail) ? data.detail.map((d) => `${d.loc.slice(1).join('.')}: ${d.msg}`).join('; ') : res.statusText);
    throw new ApiError(res.status, msg);
  }
  return data;
}

export function toast(msg) {
  const t = h('div', { class: 'toast', role: 'status' }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}

export async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message || String(e)); }
}

/** Modal form. fields: [{name,label,type,value,placeholder,options,help}] → resolves values or null. */
export function formDialog(title, fields, submitLabel = '저장') {
  return new Promise((resolve) => {
    const inputs = {};
    const rows = fields.map((f) => {
      const input = f.options
        ? h('select', {}, f.options.map((o) => h('option', { value: o.value, selected: String(o.value) === String(f.value) }, o.label)))
        : f.type === 'checkbox'
          ? h('input', { type: 'checkbox', checked: !!f.value })
          : h('input', { type: f.type || 'text', value: f.value ?? '', placeholder: f.placeholder, step: f.type === 'number' ? 'any' : null, required: f.required });
      inputs[f.name] = input;
      return h('label', {}, f.label, input, f.help && h('span', { class: 'muted' }, f.help));
    });
    const dlg = h('dialog', {}, h('form', { class: 'form', method: 'dialog' }, h('h2', {}, title), rows,
      h('div', { class: 'row' }, h('span', { class: 'spacer' }),
        h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, '취소'),
        h('button', { class: 'btn primary', type: 'submit' }, submitLabel))));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(dlg.returnValue === 'ok' ? read() : null); });
    dlg.querySelector('form').addEventListener('submit', (e) => { e.preventDefault(); dlg.returnValue = 'ok'; dlg.close('ok'); });
    function read() {
      const out = {};
      for (const f of fields) {
        const el = inputs[f.name];
        out[f.name] = f.type === 'checkbox' ? el.checked : el.value;
      }
      return out;
    }
    document.body.append(dlg);
    dlg.showModal();
  });
}

export function notice(title, ...content) {
  const dlg = h('dialog', {}, h('h2', {}, title), content, h('div', { class: 'row', style: 'margin-top:12px' },
    h('span', { class: 'spacer' }), h('button', { class: 'btn primary', onclick: () => dlg.close() }, '확인')));
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

export const confirmAction = (msg) => new Promise((resolve) => {
  const dlg = h('dialog', {}, h('p', {}, msg), h('div', { class: 'row' }, h('span', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: () => dlg.close('no') }, '취소'),
    h('button', { class: 'btn danger', onclick: () => dlg.close('yes') }, '삭제')));
  dlg.addEventListener('close', () => { dlg.remove(); resolve(dlg.returnValue === 'yes'); });
  document.body.append(dlg);
  dlg.showModal();
});

export const num = (v) => (v === '' || v == null ? null : Number(v));
export const status = (s) => h('span', { class: 'pill ' + s }, { healthy: '정상', unhealthy: '장애', unknown: '확인 전', ok: '성공', error: '실패', active: '활성', off: '비활성' }[s] || s);

/** Replace a container's children; unlike replaceChildren(), accepts nested arrays and falsy values. */
export function mount(root, ...kids) {
  root.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false).map((k) => (k.nodeType ? k : document.createTextNode(String(k)))));
}
