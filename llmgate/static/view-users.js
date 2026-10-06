import { h, mount, fmt, api, guard, formDialog, confirmAction, me, toast } from './lib.js';

const ROLES = [['viewer', '뷰어'], ['operator', '운영자'], ['admin', '관리자']];
let auditOffset = 0;
const AUDIT_PAGE = 15;

const kv = (k, v) => h('tr', {}, h('td', { class: 'ink2' }, k), h('td', {}, v ?? h('span', { class: 'muted' }, '–')));

function settingsCard(s) {
  const o = s.oidc;
  const roleMap = Object.entries(o.role_map);
  return h('div', { class: 'card' }, h('h3', {}, '인증 설정 (환경 변수로 구성)'),
    h('div', { class: 'table-wrap' }, h('table', {}, h('tbody', {},
      kv('SSO (OIDC)', h('span', { class: 'pill ' + (o.enabled ? 'ok' : 'off') }, o.enabled ? '사용 중' : '꺼짐')),
      kv('Issuer', o.issuer && h('span', { class: 'mono' }, o.issuer)),
      kv('Client ID', o.client_id && h('span', { class: 'mono' }, o.client_id)),
      kv('서명 알고리즘', o.algorithms.join(', ')),
      kv('API용 JWT audience', o.audience && h('span', { class: 'mono' }, o.audience)),
      kv('역할 클레임', h('span', { class: 'mono' }, o.roles_claim)),
      kv('기본 역할', o.default_role === 'none' ? '없음 (그룹 매핑이 없으면 로그인 거부)' : o.default_role),
      kv('허용 이메일 도메인', o.allowed_email_domains.length ? o.allowed_email_domains.join(', ') : '제한 없음'),
      kv('관리자 토큰 로그인', h('span', { class: 'pill ' + (s.admin_token_enabled ? 'ok' : 'off') }, s.admin_token_enabled ? '허용 (비상용)' : '비활성')),
      kv('세션 유지 시간', `${Math.round(s.session_ttl_s / 3600)}시간`),
      kv('추론용 JWT 클레임', h('span', { class: 'mono' }, `${s.jwt_inference.service_claim} (서비스), ${s.jwt_inference.models_claim} (모델)`))))),
    roleMap.length ? h('div', { style: 'margin-top:12px' }, h('h3', {}, 'IdP 그룹 → 역할 매핑'),
      roleMap.map(([g, r]) => h('div', {}, h('span', { class: 'tag mono' }, g), ' → ', h('strong', {}, r)))) : null);
}

export async function render(root) {
  const [settings, users, audit] = await Promise.all([
    api('GET', '/auth/settings'), api('GET', '/users'),
    api('GET', `/audit?limit=${AUDIT_PAGE}&offset=${auditOffset}`),
  ]);
  const reload = () => render(root);
  const run = (fn) => () => guard(async () => { await fn(); await reload(); });

  const userRows = users.map((u) => {
    const self = me.value && u.id === me.value.user_id;
    const select = h('select', { 'aria-label': '역할', disabled: u.pending && false, onchange: (e) => guard(async () => {
      await api('PATCH', `/users/${u.id}`, { role: e.target.value }); toast('역할을 변경했습니다.'); await reload();
    }) }, ROLES.map(([v, l]) => h('option', { value: v, selected: v === u.role }, l)));
    return h('tr', {},
      h('td', {}, h('strong', {}, u.email || u.subject), self && h('span', { class: 'tag' }, '나'), u.name && h('div', { class: 'muted' }, u.name)),
      h('td', {}, select),
      h('td', {}, u.role_source === 'manual' ? h('span', { class: 'pill warn', title: '관리자가 지정한 역할은 IdP 그룹이 바뀌어도 유지됩니다' }, '수동 지정') : h('span', { class: 'ink2' }, 'IdP 그룹'),
        u.role_source === 'manual' && !u.pending && h('button', { class: 'btn sm', style: 'margin-left:6px', onclick: run(() => api('PATCH', `/users/${u.id}`, { release_role: true })) }, 'IdP에 맡기기')),
      h('td', {}, u.pending ? h('span', { class: 'pill off' }, '로그인 대기') : h('span', { class: 'pill ' + (u.enabled ? 'active' : 'off') }, u.enabled ? '활성' : '비활성')),
      h('td', {}, fmt.time(u.last_login_at), h('div', { class: 'muted' }, `${u.login_count}회`)),
      h('td', {}, h('div', { class: 'row' },
        h('button', { class: 'btn sm', disabled: self, onclick: run(() => api('PATCH', `/users/${u.id}`, { enabled: !u.enabled })) }, u.enabled ? '비활성화' : '활성화'),
        h('button', { class: 'btn sm danger', disabled: self, onclick: () => guard(async () => {
          if (await confirmAction(`'${u.email || u.subject}' 사용자를 삭제할까요? 다음 SSO 로그인 때 다시 생성됩니다.`)) { await api('DELETE', `/users/${u.id}`); await reload(); }
        }) }, '삭제'))));
  });

  const auditRows = audit.data.map((a) => h('tr', {},
    h('td', {}, fmt.time(a.ts)), h('td', {}, a.actor, a.role && h('span', { class: 'pill ' + a.role, style: 'margin-left:6px' }, a.role)),
    h('td', {}, h('span', {}, h('span', { class: 'method ' + a.method }, a.method), h('span', { class: 'mono' }, a.path))),
    h('td', {}, h('span', { class: 'pill ' + (a.status && a.status < 400 ? 'ok' : 'error') }, a.status ?? '–')), h('td', { class: 'muted' }, a.ip)));
  const to = Math.min(auditOffset + AUDIT_PAGE, audit.total);

  mount(root,
    h('div', { class: 'row between', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '사용자 · 권한'),
      h('button', { class: 'btn primary', onclick: () => guard(async () => {
        const v = await formDialog('사용자 사전 등록', [
          { name: 'email', label: '이메일 (첫 SSO 로그인 때 이 이메일로 연결됩니다)', type: 'email', required: true },
          { name: 'role', label: '역할', options: ROLES.map(([value, label]) => ({ value, label })), value: 'viewer' }]);
        if (v) { await api('POST', '/users', v); await reload(); }
      }) }, '+ 사용자 사전 등록')),
    h('div', { class: 'grid three' }, settingsCard(settings),
      h('div', { class: 'card' }, h('h3', {}, '역할별 권한'),
        h('div', { class: 'table-wrap' }, h('table', {}, h('tbody', {}, settings.roles.map((r) => h('tr', {}, h('td', {}, h('strong', {}, r.name)), h('td', { class: 'ink2' }, r.can)))))),
        h('p', { class: 'hint' }, '권한은 서버가 모든 요청마다 검사합니다. 사용자를 비활성화하면 기존 세션도 즉시 무효가 됩니다.'))),
    h('div', { class: 'card', style: 'margin-bottom:16px' }, h('h3', {}, `사용자 (${users.length})`),
      h('div', { class: 'table-wrap' }, users.length ? h('table', {}, h('thead', {}, h('tr', {}, ['사용자', '역할', '역할 출처', '상태', '마지막 로그인', ''].map((t) => h('th', {}, t)))), h('tbody', {}, userRows))
        : h('p', { class: 'muted' }, '아직 SSO로 로그인한 사용자가 없습니다.'))),
    h('div', { class: 'card' }, h('h3', {}, `감사 로그 (${fmt.int(audit.total)}건)`),
      h('div', { class: 'table-wrap' }, audit.data.length ? h('table', {}, h('thead', {}, h('tr', {}, ['시각', '행위자', '요청', '결과', 'IP'].map((t) => h('th', {}, t)))), h('tbody', {}, auditRows))
        : h('p', { class: 'muted' }, '기록이 없습니다.')),
      h('div', { class: 'row', style: 'margin-top:12px' }, h('span', { class: 'muted' }, audit.total ? `${auditOffset + 1}–${to} / ${fmt.int(audit.total)}` : ''), h('span', { class: 'spacer' }),
        h('button', { class: 'btn sm keep', disabled: auditOffset === 0, onclick: () => { auditOffset -= AUDIT_PAGE; reload(); } }, '이전'),
        h('button', { class: 'btn sm keep', disabled: to >= audit.total, onclick: () => { auditOffset += AUDIT_PAGE; reload(); } }, '다음'))));
}
