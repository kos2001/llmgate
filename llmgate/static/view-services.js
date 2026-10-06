import { h, mount, fmt, api, guard, formDialog, confirmAction, notice, num, status } from './lib.js';

const limitFields = (o = {}) => [
  { name: 'monthly_token_limit', label: '월 토큰 한도 (비우면 무제한)', type: 'number', value: o.monthly_token_limit ?? '' },
  { name: 'monthly_budget_usd', label: '월 예산 USD (비우면 무제한)', type: 'number', value: o.monthly_budget_usd ?? '' },
];
const limits = (v) => ({ monthly_token_limit: num(v.monthly_token_limit), monthly_budget_usd: num(v.monthly_budget_usd) });
const csv = (t) => String(t ?? '').split(',').map((x) => x.trim()).filter(Boolean);
const dur = (s) => (!s ? '' : s % 86400 === 0 ? `${s / 86400}d` : s % 3600 === 0 ? `${s / 3600}h` : s % 60 === 0 ? `${s / 60}m` : `${s}s`);
const windowField = (o = {}) => ({ name: 'budget_duration', label: '예산 주기 (예: 1h, 7d, 30d — 비우면 달력 월 기준)', value: dur(o.budget_duration_s), help: '값을 바꾸면 예산 창이 지금부터 새로 시작됩니다.' });
const budgetMap = (text) => Object.fromEntries(csv(text).map((p) => p.split(':')).filter((p) => p[0] && p[1] !== undefined).map(([m, usd]) => [m.trim(), Number(usd)]));

const keyFields = (k = {}) => [
  { name: 'name', label: '키 이름', value: k.name, required: true },
  { name: 'allowed_models', label: '허용 모델 (쉼표 구분, 비우면 전체)', value: (k.allowed_models || []).join(', ') },
  { name: 'allowed_providers', label: '허용 프로바이더 (쉼표, 비우면 전체 — 요청이 넓힐 수 없는 가드레일)', value: (k.allowed_providers || []).join(', ') },
  { name: 'rpm_limit', label: '분당 요청 한도 (RPM)', type: 'number', value: k.rpm_limit ?? '' },
  { name: 'tpm_limit', label: '분당 토큰 한도 (TPM)', type: 'number', value: k.tpm_limit ?? '' },
  { name: 'max_parallel', label: '동시 요청 한도', type: 'number', value: k.max_parallel ?? '' },
  ...limitFields(k), windowField(k),
  { name: 'model_budgets', label: '모델별 예산 USD (형식: 모델:금액, 모델:금액)', value: Object.entries(k.model_budgets || {}).map(([m, b]) => `${m}:${b}`).join(', ') },
];
const keyBody = (v) => ({
  name: v.name, allowed_models: csv(v.allowed_models), allowed_providers: csv(v.allowed_providers),
  rpm_limit: num(v.rpm_limit), tpm_limit: num(v.tpm_limit), max_parallel: num(v.max_parallel), ...limits(v),
  budget_duration: v.budget_duration || null, model_budgets: Object.keys(budgetMap(v.model_budgets)).length ? budgetMap(v.model_budgets) : null,
});

function showKey(k, title) {
  notice(title, h('p', {}, '이 키는 지금 한 번만 표시됩니다. 안전한 곳에 복사해 두세요.'), h('div', { class: 'secret mono' }, k.key),
    h('p', { class: 'muted mono' }, `curl ${location.origin}/v1/chat/completions -H "Authorization: Bearer ${k.key_prefix}…"`));
}

function usageBar(used, limit, text) {
  if (!limit) return h('span', { class: 'muted' }, text + ' / 무제한');
  const ratio = used / limit;
  return h('div', { style: 'min-width:130px' }, h('span', {}, `${text} / ${limit >= 1000 ? fmt.compact(limit) : limit}`),
    h('div', { class: 'meter' }, h('i', { style: `width:${Math.min(100, ratio * 100)}%;${ratio >= 1 ? 'background:var(--critical)' : ''}` })));
}

export async function render(root) {
  const services = await api('GET', '/services');
  const reload = () => render(root);
  const run = (fn) => () => guard(async () => { await fn(); await reload(); });

  const keyRow = (s, k) => h('tr', {},
    h('td', {}, k.name, k.kind === 'jwt' ? h('span', { class: 'tag', title: 'IdP가 발급한 JWT로 인증된 신원에 자동 연결된 키' }, 'JWT') : null, h('div', { class: 'mono muted' }, k.key_prefix + '…')),
    h('td', {}, status(k.enabled ? 'active' : 'off'), k.expires_at && h('div', { class: 'muted' }, '만료 ' + fmt.time(k.expires_at))),
    h('td', { class: 'ink2 wrap' }, k.allowed_models.length ? k.allowed_models.join(', ') : '전체 모델'),
    h('td', { class: 'num' }, k.rpm_limit ?? '–', k.tpm_limit ? h('div', { class: 'muted' }, `TPM ${fmt.compact(k.tpm_limit)}`) : null, k.max_parallel ? h('div', { class: 'muted' }, `동시 ${k.max_parallel}`) : null),
    h('td', {}, usageBar(k.usage_month.tokens, k.monthly_token_limit, fmt.compact(k.usage_month.tokens) + ' tok')),
    h('td', {}, usageBar(k.usage_month.cost_usd, k.monthly_budget_usd, fmt.usd(k.usage_month.cost_usd)), k.budget_duration_s ? h('div', { class: 'muted' }, `주기 ${dur(k.budget_duration_s)}`) : null,
      Object.entries(k.model_budgets || {}).map(([m, b]) => h('div', { class: 'muted' }, `${m} ≤ ${fmt.usd(b)}`))),
    h('td', {}, fmt.ago(k.last_used_at)),
    h('td', {}, h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: () => guard(async () => {
        const v = await formDialog('키 수정', keyFields(k));
        if (v) { await api('PATCH', `/keys/${k.id}`, keyBody(v)); await reload(); }
      }) }, '수정'),
      h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/keys/${k.id}`, { enabled: !k.enabled })) }, k.enabled ? '끄기' : '켜기'),
      h('button', { class: 'btn sm', onclick: () => guard(async () => {
        if (await confirmAction('키를 재발급하면 기존 키는 즉시 무효가 됩니다. 계속할까요?')) { showKey(await api('POST', `/keys/${k.id}/rotate`), '키 재발급'); await reload(); }
      }) }, '재발급'),
      h('button', { class: 'btn sm danger', onclick: () => guard(async () => {
        if (await confirmAction(`키 '${k.name}'을 삭제할까요?`)) { await api('DELETE', `/keys/${k.id}`); await reload(); }
      }) }, '삭제'))));

  const card = (s) => h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('div', { class: 'row between' },
      h('div', {}, h('strong', {}, s.name), ' ', status(s.enabled ? 'active' : 'off'),
        h('div', { class: 'muted' }, `${s.description || ''} 이번 달 ${fmt.compact(s.usage_month.tokens)} tok · ${fmt.usd(s.usage_month.cost_usd)}`
          + (s.monthly_token_limit ? ` · 한도 ${fmt.compact(s.monthly_token_limit)} tok` : '') + (s.monthly_budget_usd != null ? ` · 예산 ${fmt.usd(s.monthly_budget_usd)}` : '')
          + (s.budget_duration_s ? ` (${dur(s.budget_duration_s)} 주기)` : '') + (s.rpm_limit ? ` · RPM ${s.rpm_limit}` : '') + (s.allowed_models.length ? ` · 모델 ${s.allowed_models.join(', ')}` : ''))),
      h('div', { class: 'row' },
        h('button', { class: 'btn sm primary', onclick: () => guard(async () => {
          const v = await formDialog('API 키 발급', keyFields());
          if (v) { showKey(await api('POST', `/services/${s.id}/keys`, keyBody(v)), 'API 키 발급 완료'); await reload(); }
        }) }, '+ 키 발급'),
        h('button', { class: 'btn sm', onclick: () => guard(async () => {
          const v = await formDialog('서비스 한도 · 접근', [{ name: 'description', label: '설명', value: s.description ?? '' },
            { name: 'allowed_models', label: '서비스 전체 허용 모델 (쉼표, 비우면 전체; 키의 목록과 교집합)', value: (s.allowed_models || []).join(', ') },
            { name: 'rpm_limit', label: '서비스 전체 RPM', type: 'number', value: s.rpm_limit ?? '' }, { name: 'tpm_limit', label: '서비스 전체 TPM', type: 'number', value: s.tpm_limit ?? '' },
            ...limitFields(s), windowField(s)]);
          if (v) { await api('PATCH', `/services/${s.id}`, { description: v.description || null, allowed_models: csv(v.allowed_models), rpm_limit: num(v.rpm_limit), tpm_limit: num(v.tpm_limit), ...limits(v), budget_duration: v.budget_duration || null }); await reload(); }
        }) }, '한도'),
        h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/services/${s.id}`, { enabled: !s.enabled })) }, s.enabled ? '끄기' : '켜기'),
        h('button', { class: 'btn sm danger', onclick: () => guard(async () => {
          if (await confirmAction(`서비스 '${s.name}'과 모든 키를 삭제할까요?`)) { await api('DELETE', `/services/${s.id}`); await reload(); }
        }) }, '삭제'))),
    s.keys.length ? h('div', { class: 'table-wrap', style: 'margin-top:8px' }, h('table', {},
      h('thead', {}, h('tr', {}, ['키', '상태', '허용 모델', 'RPM', '이번 달 토큰', '이번 달 비용', '마지막 사용', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, s.keys.map((k) => keyRow(s, k))))) : h('p', { class: 'muted' }, '발급된 키가 없습니다.'));

  mount(root, 
    h('div', { class: 'row between', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '서비스 · API 키'),
      h('button', { class: 'btn primary', onclick: () => guard(async () => {
        const v = await formDialog('서비스 추가', [{ name: 'name', label: '서비스 이름', required: true }, { name: 'description', label: '설명' }, ...limitFields()]);
        if (v) { await api('POST', '/services', { name: v.name, description: v.description || null, ...limits(v) }); await reload(); }
      }) }, '+ 서비스 추가')),
    services.length ? services.map(card) : h('div', { class: 'card' }, h('p', { class: 'muted' }, 'LLM을 사용하는 서비스(앱/팀)를 만들고 키를 발급하세요.')));
}
