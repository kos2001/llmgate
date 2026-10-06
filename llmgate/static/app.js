import { h, api, auth, me, can, icon } from './lib.js';

// Sidebar structure: category → pages. Each category owns a colour (see [data-cat] in style.css).
const NAV = [
  { cat: 'monitor', label: '모니터링', items: [['overview', '개요'], ['logs', '사용 로그']] },
  { cat: 'infra', label: '인프라', items: [['providers', '프로바이더'], ['models', '모델']] },
  { cat: 'access', label: '접근 관리', items: [['services', '서비스 · 키'], ['users', '사용자 · 권한', 'admin']] },
  { cat: 'policy', label: '정책 · 보안', items: [['controls', '제어 · 가드레일']] },
  { cat: 'tools', label: '도구', items: [['playground', '플레이그라운드']] },
];
const ROLE_LABEL = { viewer: '뷰어', operator: '운영자', admin: '관리자' };
const ROLE_COLOR = { viewer: 'aqua', operator: 'blue', admin: 'violet' };
const $ = (id) => document.getElementById(id);
const content = document.querySelector('.content');

const ss = {  // sessionStorage can be blocked; the page must still work
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } },
  del: (k) => { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
};

function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem('llmgate_theme')); } catch { /* storage may be blocked */ }
$('theme').onclick = () => {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const now = document.documentElement.dataset.theme || (dark ? 'dark' : 'light');
  const next = now === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('llmgate_theme', next); } catch { /* ignore */ }
};
$('menu').onclick = () => document.body.classList.toggle('nav-open');
$('scrim').onclick = () => document.body.classList.remove('nav-open');
$('logout').onclick = async () => {
  await api('POST', '/logout', null, { root: '/auth' }).catch(() => {});
  auth.clear();
  me.value = null;
  ss.set('llmgate_no_autologin', '1');  // after an explicit logout, show the login page instead of signing straight back in
  location.hash = '';
  boot();
};

async function fetchMe() {
  const token = auth.get();
  const res = await fetch('/auth/me', { credentials: 'same-origin', headers: token ? { Authorization: 'Bearer ' + token } : {} });
  return res.ok ? res.json() : null;
}

function loginForm(cfg, notice) {
  $('app').hidden = true;
  document.body.dataset.role = '';
  const here = '/dashboard/' + location.hash;
  const input = h('input', { type: 'password', placeholder: 'LLMGATE_ADMIN_TOKEN', autocomplete: 'current-password' });
  const err = h('p', { class: 'muted', role: 'alert' }, notice || '');
  const parts = [h('div', { class: 'row' }, h('img', { class: 'logo', src: 'favicon.svg', alt: '' }), h('h2', { style: 'margin:0' }, 'llmgate'))];
  if (cfg.oidc.enabled) {
    parts.push(h('a', { class: 'btn primary', href: `${cfg.oidc.login_url}?next=${encodeURIComponent(here)}`, style: 'text-align:center' }, 'SSO로 로그인'));
    if (cfg.dev_autologin) {
      parts.push(h('a', { class: 'btn', href: `${cfg.oidc.login_url}?prompt=select_account&next=${encodeURIComponent(here)}`, style: 'text-align:center' }, '다른 사용자로 로그인'));
    }
  }
  if (cfg.admin_token) {
    parts.push(cfg.oidc.enabled ? h('p', { class: 'muted', style: 'text-align:center;margin:4px 0' }, '또는 관리자 토큰') : null,
      h('label', {}, '관리자 토큰', input), h('button', { class: 'btn' + (cfg.oidc.enabled ? '' : ' primary'), type: 'submit' }, '토큰으로 로그인'));
  }
  if (!cfg.oidc.enabled && !cfg.admin_token) parts.push(h('p', {}, '로그인 방법이 설정되어 있지 않습니다. LLMGATE_OIDC_* 또는 LLMGATE_ADMIN_TOKEN을 설정하세요.'));
  const form = h('form', { class: 'card login form', 'data-cat': 'monitor' }, ...parts, err);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    auth.set(input.value);
    if (await fetchMe()) boot(); else { auth.clear(); err.textContent = '토큰이 올바르지 않습니다.'; }
  });
  $('loginview').hidden = false;
  $('loginview').replaceChildren(form);
  (input.isConnected && cfg.admin_token && !cfg.oidc.enabled ? input : form.querySelector('a, input'))?.focus();
}

function buildSidebar() {
  const nav = $('nav');
  nav.replaceChildren(...NAV.map((g) => {
    const items = g.items.filter(([, , role]) => can(role || 'viewer'));
    if (!items.length) return null;
    return h('section', { class: 'nav-group', 'data-cat': g.cat }, h('h4', {}, g.label),
      ...items.map(([id, label]) => h('button', { class: 'nav-item', 'data-v': id, 'data-cat': g.cat, onclick: () => {
        location.hash = id; document.body.classList.remove('nav-open');
      } }, h('span', { class: 'ico' }, icon(id)), label)));
  }).filter(Boolean));

  const color = ROLE_COLOR[me.value.role] || 'blue';
  const name = me.value.email || me.value.name || (me.value.kind === 'admin_token' ? '관리자 토큰' : me.value.subject);
  $('sidefoot').replaceChildren(
    h('span', { class: 'avatar', style: `--cat:var(--${color});--on-cat:var(--on-${color})` }, name.slice(0, 1).toUpperCase()),
    h('div', { class: 'who' }, h('b', { title: name }, name), h('span', { class: 'pill ' + me.value.role, title: `${me.value.kind} · ${me.value.subject}` }, ROLE_LABEL[me.value.role] || me.value.role)));
}

async function show(name) {
  const groups = NAV.map((g) => ({ ...g, items: g.items.filter(([, , role]) => can(role || 'viewer')) })).filter((g) => g.items.length);
  const found = groups.flatMap((g) => g.items.map((i) => ({ g, id: i[0], label: i[1] }))).find((x) => x.id === name) || { g: groups[0], id: groups[0].items[0][0], label: groups[0].items[0][1] };
  content.dataset.cat = found.g.cat;
  document.querySelectorAll('#nav .nav-item').forEach((b) => {
    if (b.dataset.v === found.id) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  $('crumb').replaceChildren(h('span', { class: 'dot' }), found.g.label, ' / ', h('b', {}, found.label));
  const holder = h('div', { 'data-view': found.id });
  $('view').replaceChildren(holder);
  try {
    await (await import(`./view-${found.id}.js`)).render(holder);
  } catch (e) {
    if (e.status === 401) { auth.clear(); me.value = null; boot(); return; }
    holder.replaceChildren(h('div', { class: 'card' }, h('p', {}, '불러오지 못했습니다: ' + e.message)));
  }
}

async function boot() {
  const cfg = await fetch('/auth/config').then((r) => r.json()).catch(() => ({ oidc: { enabled: false }, admin_token: true }));
  const params = new URLSearchParams(location.search);
  const failure = params.get('auth_error');
  if (failure) history.replaceState(null, '', location.pathname + location.hash);
  me.value = await fetchMe();

  if (!me.value) {
    // Dev mode: skip the form and go straight to SSO. Guarded so a broken cookie can never cause a redirect loop.
    const justTried = Date.now() - Number(ss.get('llmgate_autologin_try') || 0) < 30000;
    if (cfg.dev_autologin && !failure && ss.get('llmgate_no_autologin') !== '1' && !justTried) {
      ss.set('llmgate_autologin_try', String(Date.now()));
      location.replace(`${cfg.oidc.login_url}?next=${encodeURIComponent('/dashboard/' + location.hash)}`);
      return;
    }
    return loginForm(cfg, failure ? 'SSO 로그인 실패: ' + failure : (cfg.dev_autologin && justTried ? '자동 로그인에 실패했습니다. 쿠키가 차단되었는지 확인하세요.' : ''));
  }
  ss.del('llmgate_autologin_try');
  ss.del('llmgate_no_autologin');

  $('loginview').hidden = true;
  $('app').hidden = false;
  $('envbadge').hidden = !cfg.dev_autologin;
  document.body.dataset.role = me.value.role;
  buildSidebar();
  show(location.hash.slice(1));
}
addEventListener('hashchange', () => me.value && show(location.hash.slice(1)));
boot();
