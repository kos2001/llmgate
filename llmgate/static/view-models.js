import { h, mount, fmt, api, guard, formDialog, confirmAction, num, status } from './lib.js';

const modelFields = (m = {}) => [
  { name: 'name', label: '공개 모델 ID (클라이언트가 model에 넣는 값)', value: m.name, required: true },
  { name: 'display_name', label: '표시 이름', value: m.display_name ?? '' },
  { name: 'kind', label: '종류', options: [{ value: 'chat', label: 'chat / completions' }, { value: 'embedding', label: 'embedding' }], value: m.kind || 'chat' },
  { name: 'context_length', label: '컨텍스트 길이', type: 'number', value: m.context_length ?? '' },
  { name: 'input_price_per_m', label: '입력 단가 (USD / 1M 토큰)', type: 'number', value: m.input_price_per_m ?? 0 },
  { name: 'output_price_per_m', label: '출력 단가 (USD / 1M 토큰)', type: 'number', value: m.output_price_per_m ?? 0 },
  { name: 'max_completion_tokens', label: '최대 출력 토큰', type: 'number', value: m.max_completion_tokens ?? '' },
  { name: 'supported_parameters', label: '지원 파라미터 (쉼표: tools, response_format, structured_outputs, reasoning, logprobs, seed, stop — 비우면 미지정)', value: (m.supported_parameters || []).join(', ') },
  { name: 'input_modalities', label: '입력 모달리티 (쉼표: text, image, audio, file)', value: (m.input_modalities || ['text']).join(', ') },
  { name: 'routing_strategy', label: '라우팅 전략', options: [['weighted', 'weighted — 우선순위 + 가중치'], ['least-busy', 'least-busy — 진행 중 요청이 적은 곳'], ['latency', 'latency — 응답이 빠른 곳'], ['cost', 'cost — 가장 싼 곳'], ['usage', 'usage — 최근 토큰 사용이 적은 곳']].map(([value, label]) => ({ value, label })), value: m.routing_strategy || 'weighted' },
  { name: 'fallbacks', label: '실패 시 폴백 모델 (쉼표, 순서대로)', value: (m.fallbacks || []).join(', ') },
  { name: 'context_window_fallbacks', label: '컨텍스트 초과 시 폴백 모델 (쉼표)', value: (m.context_window_fallbacks || []).join(', ') },
  { name: 'description', label: '설명', value: m.description ?? '' },
];
const csv = (s) => String(s ?? '').split(',').map((x) => x.trim()).filter(Boolean);

const modelBody = (v) => ({
  name: v.name, display_name: v.display_name || null, kind: v.kind, description: v.description || null,
  context_length: num(v.context_length), input_price_per_m: Number(v.input_price_per_m || 0), output_price_per_m: Number(v.output_price_per_m || 0),
  max_completion_tokens: num(v.max_completion_tokens), supported_parameters: csv(v.supported_parameters).length ? csv(v.supported_parameters) : null,
  input_modalities: csv(v.input_modalities).length ? csv(v.input_modalities) : ['text'], routing_strategy: v.routing_strategy,
  fallbacks: csv(v.fallbacks), context_window_fallbacks: csv(v.context_window_fallbacks),
});

export async function render(root) {
  const [models, providers] = await Promise.all([api('GET', '/models'), api('GET', '/providers')]);
  const reload = () => render(root);
  const run = (fn) => () => guard(async () => { await fn(); await reload(); });

  const routeFields = (r = {}) => [
    ...(r.id ? [] : [{ name: 'provider_id', label: '프로바이더', options: providers.map((p) => ({ value: p.id, label: `${p.name} (${p.kind})` })) }]),
    { name: 'upstream_model', label: '엔진이 아는 모델 이름', value: r.upstream_model, required: true },
    { name: 'priority', label: '우선순위 (낮을수록 먼저, 같은 값끼리 부하분산)', type: 'number', value: r.priority ?? 0 },
    { name: 'weight', label: '가중치 (같은 우선순위 안에서의 비율)', type: 'number', value: r.weight ?? 1 },
    { name: 'input_price_per_m', label: '이 프로바이더 입력 단가 USD/1M (비우면 모델 단가)', type: 'number', value: r.input_price_per_m ?? '' },
    { name: 'output_price_per_m', label: '이 프로바이더 출력 단가 USD/1M', type: 'number', value: r.output_price_per_m ?? '' },
    { name: 'rpm_limit', label: '라우트 RPM 한도 (넘으면 다른 라우트로)', type: 'number', value: r.rpm_limit ?? '' },
    { name: 'tpm_limit', label: '라우트 TPM 한도', type: 'number', value: r.tpm_limit ?? '' },
    { name: 'max_parallel', label: '라우트 동시 요청 한도', type: 'number', value: r.max_parallel ?? '' },
  ];
  const routeBody = (v) => ({ upstream_model: v.upstream_model, priority: Number(v.priority), weight: Number(v.weight),
    input_price_per_m: num(v.input_price_per_m), output_price_per_m: num(v.output_price_per_m),
    rpm_limit: num(v.rpm_limit), tpm_limit: num(v.tpm_limit), max_parallel: num(v.max_parallel) });

  const card = (m) => h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('div', { class: 'row between' },
      h('div', {}, h('strong', {}, m.name), ' ', h('span', { class: 'pill ' + (m.enabled ? 'ok' : 'off') }, m.enabled ? m.kind : '비활성'),
        h('div', { class: 'muted' }, h('span', { class: 'tag' }, m.routing_strategy), (m.supported_parameters || []).map((p) => h('span', { class: 'tag' }, p)), m.input_modalities.filter((x) => x !== 'text').map((x) => h('span', { class: 'tag' }, '입력:' + x)),
          m.fallbacks.length ? h('span', { class: 'tag' }, '폴백: ' + m.fallbacks.join(' → ')) : null, m.context_window_fallbacks.length ? h('span', { class: 'tag' }, '컨텍스트 폴백: ' + m.context_window_fallbacks.join(' → ')) : null),
        h('div', { class: 'muted' }, `컨텍스트 ${m.context_length ? fmt.int(m.context_length) : '–'} · 입력 ${fmt.usd(m.input_price_per_m)} / 출력 ${fmt.usd(m.output_price_per_m)} (1M 토큰) · 30일 ${fmt.compact(m.usage_30d.tokens)} tok · ${fmt.usd(m.usage_30d.cost_usd)}`)),
      h('div', { class: 'row' },
        h('button', { class: 'btn sm', onclick: () => guard(async () => {
          const v = await formDialog('모델 수정', modelFields(m));
          if (v) { await api('PATCH', `/models/${m.id}`, modelBody(v)); await reload(); }
        }) }, '수정'),
        h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/models/${m.id}`, { enabled: !m.enabled })) }, m.enabled ? '끄기' : '켜기'),
        h('button', { class: 'btn sm danger', onclick: () => guard(async () => {
          if (await confirmAction(`모델 '${m.name}'을 삭제할까요?`)) { await api('DELETE', `/models/${m.id}`); await reload(); }
        }) }, '삭제'))),
    h('div', { class: 'table-wrap', style: 'margin-top:8px' }, h('table', {},
      h('thead', {}, h('tr', {}, ['프로바이더', '상태', '업스트림 모델', '우선순위', '가중치', '단가 / 한도', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, m.routes.map((r) => h('tr', {},
        h('td', {}, r.provider_name), h('td', {}, status(r.enabled ? r.provider_status : 'off')), h('td', { class: 'mono' }, r.upstream_model),
        h('td', {}, r.priority), h('td', {}, r.weight),
        h('td', { class: 'muted' }, [r.input_price_per_m != null || r.output_price_per_m != null ? `$${r.input_price_per_m ?? m.input_price_per_m}/${r.output_price_per_m ?? m.output_price_per_m}` : null,
          r.rpm_limit ? `RPM ${r.rpm_limit}` : null, r.tpm_limit ? `TPM ${r.tpm_limit}` : null, r.max_parallel ? `동시 ${r.max_parallel}` : null].filter(Boolean).join(' · ') || '–'),
        h('td', {}, h('div', { class: 'row' },
          h('button', { class: 'btn sm', onclick: () => guard(async () => {
            const v = await formDialog('라우트 수정', routeFields(r));
            if (v) { await api('PATCH', `/routes/${r.id}`, routeBody(v)); await reload(); }
          }) }, '수정'),
          h('button', { class: 'btn sm', onclick: run(() => api('PATCH', `/routes/${r.id}`, { enabled: !r.enabled })) }, r.enabled ? '끄기' : '켜기'),
          h('button', { class: 'btn sm danger', onclick: run(() => api('DELETE', `/routes/${r.id}`)) }, '삭제'))))))),
      !m.routes.length && h('p', { class: 'muted' }, '라우트가 없어 요청을 처리할 수 없습니다.')),
    h('button', { class: 'btn sm', style: 'margin-top:8px', disabled: !providers.length, onclick: () => guard(async () => {
      const v = await formDialog('라우트 추가', routeFields({ upstream_model: m.name }));
      if (v) { await api('POST', `/models/${m.id}/routes`, { provider_id: Number(v.provider_id), ...routeBody(v) }); await reload(); }
    }) }, '+ 라우트 추가'));

  mount(root, 
    h('div', { class: 'row between', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '모델 레지스트리'),
      h('button', { class: 'btn primary', onclick: () => guard(async () => {
        const v = await formDialog('모델 추가', modelFields());
        if (v) { await api('POST', '/models', modelBody(v)); await reload(); }
      }) }, '+ 모델 추가')),
    models.length ? models.map(card) : h('div', { class: 'card' }, h('p', { class: 'muted' }, '등록된 모델이 없습니다. 프로바이더 탭에서 "모델 동기화"로 엔진이 서빙 중인 모델을 가져올 수 있습니다.')));
}
