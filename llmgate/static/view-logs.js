import { h, mount, fmt, api, status, download, guard } from './lib.js';

const state = { days: 7, group: 'model', service: '', model: '', errors: false, offset: 0 };
const PAGE = 25;
const GROUPS = { model: '모델', service: '서비스', key: '키', provider: '프로바이더', endpoint: '엔드포인트', app: '앱 (X-Title)', user: '최종 사용자' };

export async function render(root) {
  const qs = new URLSearchParams({ days: state.days });
  if (state.service) qs.set('service_id', state.service);
  if (state.model) qs.set('model', state.model);
  const [services, models, summary, logs] = await Promise.all([
    api('GET', '/services'), api('GET', '/models'),
    api('GET', `/usage/summary?${qs}&group_by=${state.group}`),
    api('GET', `/usage/logs?${qs}&limit=${PAGE}&offset=${state.offset}&only_errors=${state.errors}`),
  ]);
  const redo = (patch) => { Object.assign(state, { offset: 0 }, patch); render(root); };
  const sel = (key, opts, label) => h('select', { 'aria-label': label, onchange: (e) => redo({ [key]: e.target.value }) },
    opts.map(([v, l]) => h('option', { value: v, selected: String(v) === String(state[key]) }, l)));

  const sumTable = h('table', {}, h('thead', {}, h('tr', {}, [GROUPS[state.group], '요청', '오류', '입력', '출력', '합계', '비용', '평균 지연'].map((t, i) => h('th', { class: i ? 'num' : '' }, t)))),
    h('tbody', {}, summary.map((r) => h('tr', {}, h('td', {}, r.name), h('td', { class: 'num' }, fmt.int(r.requests)), h('td', { class: 'num' }, fmt.int(r.errors)),
      h('td', { class: 'num' }, fmt.int(r.prompt_tokens)), h('td', { class: 'num' }, fmt.int(r.completion_tokens)), h('td', { class: 'num' }, fmt.int(r.total_tokens)),
      h('td', { class: 'num' }, fmt.usd(r.cost_usd)), h('td', { class: 'num' }, fmt.ms(r.avg_latency_ms))))));

  const logTable = h('table', {}, h('thead', {}, h('tr', {}, ['시각', '서비스 / 키', '모델', '프로바이더', '입력', '출력', '비용', '지연 / TTFT', '결과'].map((t, i) => h('th', { class: i > 3 && i < 8 ? 'num' : '' }, t)))),
    h('tbody', {}, logs.data.map((l) => h('tr', { title: l.error || '' },
      h('td', {}, fmt.time(l.ts)), h('td', {}, `${l.service_name ?? '–'} / ${l.key_name ?? '–'}`, (l.app_title || l.end_user) && h('div', { class: 'muted' }, [l.app_title, l.end_user && `user:${l.end_user}`].filter(Boolean).join(' · '))), h('td', {}, l.model ?? '–', l.stream ? ' ⇢' : '', l.requested_model && h('div', { class: 'muted' }, `← ${l.requested_model} 폴백`),
        l.cache_hit ? h('span', { class: 'tag' }, '캐시') : null, l.guardrail ? h('span', { class: 'tag', title: '가드레일' }, '🛡 ' + l.guardrail) : null,
        ...(l.tags ? JSON.parse(l.tags).map((t) => h('span', { class: 'tag' }, '#' + t)) : [])),
      h('td', {}, l.provider_name ?? '–', l.attempts > 1 && h('span', { class: 'muted' }, ` (재시도 ${l.attempts - 1})`)),
      h('td', { class: 'num' }, fmt.int(l.prompt_tokens), l.usage_estimated ? '~' : ''), h('td', { class: 'num' }, fmt.int(l.completion_tokens), l.usage_estimated ? '~' : ''),
      h('td', { class: 'num' }, fmt.usd(l.cost_usd)), h('td', { class: 'num' }, fmt.ms(l.latency_ms), l.ttft_ms != null && h('span', { class: 'muted' }, ' / ' + fmt.ms(l.ttft_ms))),
      h('td', {}, h('span', { class: 'pill ' + (l.success ? 'ok' : 'error') }, l.status_code), l.error && h('div', { class: 'muted' }, l.error.slice(0, 48)))))));

  const to = Math.min(state.offset + PAGE, logs.total);
  mount(root, 
    h('div', { class: 'row', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '사용량'), h('span', { class: 'spacer' }),
      sel('days', [[1, '24시간'], [7, '7일'], [30, '30일'], [90, '90일']], '기간'),
      sel('service', [['', '모든 서비스'], ...services.map((s) => [s.id, s.name])], '서비스'),
      sel('model', [['', '모든 모델'], ...models.map((m) => [m.name, m.name])], '모델')),
    h('div', { class: 'card', style: 'margin-bottom:16px' },
      h('div', { class: 'row between' }, h('h3', {}, '집계'), sel('group', Object.entries(GROUPS), '그룹 기준')),
      h('div', { class: 'table-wrap' }, summary.length ? sumTable : h('p', { class: 'muted' }, '해당 기간의 데이터가 없습니다.'))),
    h('div', { class: 'card' },
      h('div', { class: 'row between' }, h('h3', {}, `요청 로그 (${fmt.int(logs.total)}건)`),
        h('div', { class: 'row' },
          ...['csv', 'jsonl'].map((f) => h('button', { class: 'btn sm keep', title: '현재 기간·필터로 내보내기', onclick: () => guard(() => download(`/usage/export?${qs}&only_errors=${state.errors}&format=${f}`, `llmgate-usage.${f}`)) }, f === 'csv' ? 'CSV 내보내기' : 'JSONL')),
        h('label', { class: 'row ink2' }, h('input', { type: 'checkbox', checked: state.errors, onchange: (e) => redo({ errors: e.target.checked }) }), '오류만'))),
      h('div', { class: 'table-wrap' }, logs.data.length ? logTable : h('p', { class: 'muted' }, '로그가 없습니다.')),
      h('div', { class: 'row', style: 'margin-top:12px' }, h('span', { class: 'muted' }, logs.total ? `${state.offset + 1}–${to} / ${fmt.int(logs.total)}  (~ 는 엔진이 usage를 주지 않아 추정한 값)` : ''),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn sm keep', disabled: state.offset === 0, onclick: () => { state.offset -= PAGE; render(root); } }, '이전'),
        h('button', { class: 'btn sm keep', disabled: to >= logs.total, onclick: () => { state.offset += PAGE; render(root); } }, '다음'))));
}
