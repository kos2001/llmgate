import { h, mount, fmt, api, guard, formDialog, confirmAction, toast, num, bytes, download } from './lib.js';
import { rankBars } from './charts.js';

const KINDS = {
  pii: ['개인정보 (이메일·전화·카드·주민번호·SSN)', '{"entities":["EMAIL","PHONE","CREDIT_CARD","KR_RRN","US_SSN"]}'],
  secrets: ['시크릿 (API 키·토큰·개인키)', '{}'],
  keywords: ['금칙어', '{"words":["기밀프로젝트"],"case_sensitive":false}'],
  regex: ['정규식', '{"pattern":"TICKET-\\\\d+","label":"TICKET"}'],
  injection: ['프롬프트 인젝션 탐지', '{}'],
  max_chars: ['입력 길이 제한', '{"limit":20000}'],
};
const WHERE = { input: '입력', output: '출력(비스트리밍)', both: '입력+출력' };
const section = (title, ...kids) => h('div', { class: 'card' }, h('h3', {}, title), ...kids);
const tbl = (head, rows, empty) => rows.length
  ? h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, head.map((t) => h('th', {}, t)))), h('tbody', {}, rows)))
  : h('p', { class: 'muted' }, empty);

async function guardrailForm(rule) {
  const kind = rule?.kind;
  const v = await formDialog(rule ? '가드레일 수정' : '가드레일 추가', [
    { name: 'name', label: '이름', value: rule?.name, required: true },
    ...(rule ? [] : [{ name: 'kind', label: '종류', options: Object.entries(KINDS).map(([value, [label]]) => ({ value, label })), value: 'pii' }]),
    { name: 'mode', label: '동작', options: [{ value: 'block', label: 'block — 요청 거부(400)' }, { value: 'mask', label: 'mask — 가려서 통과' }], value: rule?.mode || 'block' },
    { name: 'apply_to', label: '적용 대상', options: Object.entries(WHERE).map(([value, label]) => ({ value, label })), value: rule?.apply_to || 'input' },
    { name: 'config', label: '설정 (JSON)', value: rule ? JSON.stringify(rule.config) : KINDS.pii[1], help: rule ? undefined : '종류에 맞게 수정하세요: ' + Object.entries(KINDS).map(([k, [, c]]) => `${k} ${c}`).join('  |  ') },
    { name: 'default_on', label: '모든 요청에 기본 적용 (해제하면 요청이 "guardrails":[이름]으로 지정할 때만)', type: 'checkbox', value: rule ? rule.default_on : true },
  ]);
  if (!v) return null;
  let config;
  try { config = JSON.parse(v.config || '{}'); } catch { throw new Error('설정이 올바른 JSON이 아닙니다.'); }
  return { ...v, config, kind: kind || v.kind };
}

export async function render(root) {
  const [rules, cache, aliases, customers, models, tags, store] = await Promise.all([
    api('GET', '/guardrails'), api('GET', '/cache'), api('GET', '/aliases'), api('GET', '/customers'),
    api('GET', '/models'), api('GET', '/usage/tags?days=30'), api('GET', '/storage'),
  ]);
  const reload = () => render(root);
  const run = (fn) => () => guard(async () => { await fn(); await reload(); });

  // ---- guardrails
  const probe = h('textarea', { rows: 3, placeholder: '예: 문의는 kim@example.com 으로, 카드 4111 1111 1111 1111' });
  const probeOut = h('div', { class: 'muted', style: 'margin-top:8px;white-space:pre-wrap' });
  const guardrails = section('가드레일 (입력 · 출력 검사)',
    tbl(['이름', '종류', '동작', '대상', '기본 적용', '상태', ''], rules.map((r) => h('tr', {},
      h('td', {}, h('strong', {}, r.name), h('div', { class: 'mono muted' }, JSON.stringify(r.config).slice(0, 60))),
      h('td', {}, r.kind), h('td', {}, h('span', { class: 'pill ' + (r.mode === 'block' ? 'error' : 'ok') }, r.mode)), h('td', {}, WHERE[r.apply_to]),
      h('td', {}, r.default_on ? '예' : '요청 지정 시'), h('td', {}, h('span', { class: 'pill ' + (r.enabled ? 'active' : 'off') }, r.enabled ? '켜짐' : '꺼짐')),
      h('td', {}, h('div', { class: 'row' },
        h('button', { class: 'btn sm', onclick: () => guard(async () => { const v = await guardrailForm(r); if (v) { await api('PATCH', `/guardrails/${r.id}`, v); await reload(); } }) }, '수정'),
        h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/guardrails/${r.id}`, { enabled: !r.enabled })) }, r.enabled ? '끄기' : '켜기'),
        h('button', { class: 'btn sm danger', onclick: run(() => api('DELETE', `/guardrails/${r.id}`)) }, '삭제'))))), '등록된 가드레일이 없습니다.'),
    h('button', { class: 'btn sm primary', style: 'margin-top:8px', onclick: () => guard(async () => { const v = await guardrailForm(); if (v) { await api('POST', '/guardrails', v); await reload(); } }) }, '+ 가드레일 추가'),
    h('p', { class: 'hint' }, '출력 검사는 완성된 응답에만 적용됩니다. 스트리밍 응답은 클라이언트로 나가기 전에 검사할 수 없습니다.'),
    h('h3', { style: 'margin-top:12px' }, '시험기 (모델을 호출하지 않습니다)'), probe,
    h('button', { class: 'btn sm keep', style: 'margin-top:6px', onclick: () => guard(async () => {
      const r = await api('POST', '/guardrails/test', { text: probe.value });
      probeOut.textContent = r.hits.length ? `${r.blocked ? '⛔ 차단됨' : '✅ 통과(가려짐)'}\n` + r.hits.map((x) => `· ${x.rule} (${x.mode}): ${x.found.join(', ')}`).join('\n') + (r.masked_text ? `\n→ ${r.masked_text}` : '') : '✅ 걸리는 규칙이 없습니다.';
    }) }, '시험'), probeOut);

  // ---- cache
  const cacheCard = section('응답 캐시',
    h('div', { class: 'row', style: 'gap:24px' }, ...[['항목', cache.entries], ['적중', cache.hits], ['미스', cache.misses], ['적중률', cache.hit_rate == null ? '–' : fmt.pct(cache.hit_rate)]]
      .map(([l, v]) => h('div', {}, h('div', { class: 'muted' }, l), h('div', { style: 'font-size:22px;font-weight:600' }, typeof v === 'number' ? fmt.int(v) : v)))),
    h('p', { class: 'hint' }, cache.ttl_s ? `전역 TTL ${cache.ttl_s}초 · 범위: ${cache.scope === 'service' ? '서비스별' : '전체 공유'}` : '전역 캐시는 꺼져 있습니다(LLMGATE_CACHE_TTL_S). 요청에 "cache":{"ttl":60}을 넣으면 그 요청만 캐시됩니다.'),
    h('button', { class: 'btn sm', onclick: run(async () => { const r = await api('POST', '/cache/flush'); toast(`${r.flushed}개 항목을 비웠습니다.`); }) }, '캐시 비우기'));

  // ---- aliases
  const aliasCard = section('모델 별칭',
    tbl(['별칭', '→ 모델', ''], aliases.map((a) => h('tr', {}, h('td', { class: 'mono' }, a.alias), h('td', {}, a.model_name),
      h('td', {}, h('button', { class: 'btn sm danger', onclick: run(() => api('DELETE', `/aliases/${encodeURIComponent(a.alias)}`)) }, '삭제')))), '별칭이 없습니다.'),
    h('button', { class: 'btn sm', style: 'margin-top:8px', disabled: !models.length, onclick: () => guard(async () => {
      const v = await formDialog('모델 별칭 추가', [{ name: 'alias', label: '별칭 (클라이언트가 계속 쓰는 이름, 예: gpt-4)', required: true },
        { name: 'model_name', label: '실제 모델', options: models.map((m) => ({ value: m.name, label: m.name })) }]);
      if (v) { await api('POST', '/aliases', v); await reload(); }
    }) }, '+ 별칭 추가'));

  // ---- customers
  const customerCard = section('최종 사용자(고객) 예산 — 요청의 "user" 필드 기준',
    tbl(['user', '별칭', '지출 (예산 창)', '예산', '상태', ''], customers.map((c) => h('tr', {},
      h('td', {}, h('span', { class: 'mono' }, c.user_id), c.unregistered && h('span', { class: 'tag' }, '미등록')), h('td', {}, c.alias || '–'),
      h('td', {}, fmt.usd(c.usage.cost_usd), h('div', { class: 'muted' }, fmt.compact(c.usage.tokens) + ' tok')),
      h('td', {}, c.max_budget_usd == null ? '무제한' : fmt.usd(c.max_budget_usd)),
      h('td', {}, h('span', { class: 'pill ' + (c.blocked ? 'error' : 'active') }, c.blocked ? '차단' : '정상')),
      h('td', {}, h('div', { class: 'row' },
        h('button', { class: 'btn sm', onclick: () => guard(async () => {
          const v = await formDialog(c.unregistered ? '예산 설정' : '고객 수정', [{ name: 'max_budget_usd', label: '예산 USD (비우면 무제한)', type: 'number', value: c.max_budget_usd ?? '' },
            { name: 'budget_duration', label: '예산 주기 (예: 30d, 비우면 달력 월)', value: '' }, { name: 'alias', label: '별칭', value: c.alias ?? '' }]);
          if (!v) return;
          const body = { max_budget_usd: num(v.max_budget_usd), alias: v.alias || null, ...(v.budget_duration ? { budget_duration: v.budget_duration } : {}) };
          if (c.unregistered) await api('POST', '/customers', { user_id: c.user_id, ...body }); else await api('PATCH', `/customers/${encodeURIComponent(c.user_id)}`, body);
          await reload();
        }) }, c.unregistered ? '예산 설정' : '수정'),
        !c.unregistered && h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/customers/${encodeURIComponent(c.user_id)}`, { blocked: !c.blocked })) }, c.blocked ? '차단 해제' : '차단'),
        !c.unregistered && h('button', { class: 'btn sm danger', onclick: () => guard(async () => { if (await confirmAction(`'${c.user_id}'를 삭제할까요?`)) { await api('DELETE', `/customers/${encodeURIComponent(c.user_id)}`); await reload(); } }) }, '삭제'))))),
      '"user" 필드를 보낸 요청이 아직 없습니다.'),
    h('button', { class: 'btn sm', style: 'margin-top:8px', onclick: () => guard(async () => {
      const v = await formDialog('고객 추가', [{ name: 'user_id', label: 'user ID', required: true }, { name: 'max_budget_usd', label: '예산 USD', type: 'number' }]);
      if (v) { await api('POST', '/customers', { user_id: v.user_id, max_budget_usd: num(v.max_budget_usd) }); await reload(); }
    }) }, '+ 고객 추가'));

  const tagCard = section('태그별 지출 (30일) — 요청의 "tags" / metadata.tags',
    rankBars(tags, { value: (t) => t.cost_usd, format: (n) => fmt.usd(n), tip: (t) => [t.name, `요청 ${fmt.int(t.requests)}`, `토큰 ${fmt.int(t.total_tokens)}`, `비용 ${fmt.usd(t.cost_usd)}`] }));

  const day = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString('ko-KR') : '–');
  const storageCard = section('저장소 · 데이터 보존',
    h('div', { class: 'row', style: 'gap:24px' }, ...[['DB 크기', bytes(store.size_bytes)], ['사용 로그', fmt.int(store.rows.usage_logs)], ['감사 로그', fmt.int(store.rows.audit_logs)]]
      .map(([l, v]) => h('div', {}, h('div', { class: 'muted' }, l), h('div', { style: 'font-size:20px;font-weight:600' }, v)))),
    h('p', { class: 'hint' }, `가장 오래된 기록 ${day(store.oldest_usage_ts)} · 자동 삭제 ${store.retention.usage_days ? store.retention.usage_days + '일 보존' : '꺼짐 (LLMGATE_USAGE_RETENTION_DAYS)'}`),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm keep', onclick: () => guard(() => download('/usage/export?days=3660&format=csv', 'llmgate-usage.csv')) }, '전체 CSV 내보내기'),
      h('button', { class: 'btn sm danger adm', onclick: () => guard(async () => {
        const v = await formDialog('오래된 사용 로그 삭제', [{ name: 'days', label: '이 일수보다 오래된 사용 로그를 삭제합니다', type: 'number', value: 90 }]);
        if (v && await confirmAction(`${v.days}일보다 오래된 사용 로그를 삭제할까요? 되돌릴 수 없습니다.`)) {
          const r = await api('POST', `/storage/prune?days=${Number(v.days)}`); toast(`${fmt.int(r.usage_logs)}건 삭제`); await reload();
        }
      }) }, '오래된 로그 삭제')));

  mount(root, h('h2', {}, '제어 · 가드레일'), h('div', { style: 'margin-bottom:16px' }, guardrails),
    h('div', { class: 'grid wide' }, cacheCard, storageCard, tagCard), h('div', { class: 'grid wide' }, customerCard, aliasCard));
}
