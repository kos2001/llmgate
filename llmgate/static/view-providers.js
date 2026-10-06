import { h, mount, fmt, api, guard, formDialog, confirmAction, notice, toast, num, status } from './lib.js';

const KINDS = ['vllm', 'ollama', 'tgi', 'openai', 'other'].map((k) => ({ value: k, label: k }));

const fields = (p = {}) => [
  { name: 'name', label: '이름', value: p.name, required: true },
  { name: 'kind', label: '엔진 종류', options: KINDS, value: p.kind || 'vllm' },
  { name: 'base_url', label: 'Base URL (OpenAI 호환, /v1 포함)', value: p.base_url, placeholder: 'http://gpu-1:8000/v1', required: true },
  { name: 'api_key', label: '업스트림 API 키 (선택)', type: 'password', placeholder: p.has_api_key ? '변경하려면 입력 (비우면 유지)' : '' },
  { name: 'timeout_s', label: '읽기 타임아웃(초, 비우면 기본값)', type: 'number', value: p.timeout_s ?? '' },
  { name: 'data_collection', label: '프롬프트 수집 정책 (이 엔진이 요청 내용을 저장/학습에 쓰나요?)', options: [{ value: 'deny', label: 'deny — 저장하지 않음' }, { value: 'allow', label: 'allow — 저장될 수 있음' }], value: p.data_collection || 'deny' },
  { name: 'quantization', label: '양자화 (예: fp8, int4, bf16 — provider.quantizations 필터용)', value: p.quantization ?? '' },
];

function body(v, editing) {
  const out = { name: v.name, kind: v.kind, base_url: v.base_url, timeout_s: num(v.timeout_s), data_collection: v.data_collection, quantization: v.quantization || null };
  if (v.api_key || !editing) out.api_key = v.api_key || null;
  return out;
}

function engine(p) {
  const s = p.engine_stats;
  if (!s) return h('span', { class: 'muted' }, '엔진 지표 없음');
  const kv = s.kv_cache_usage ?? 0;
  return h('div', { style: 'min-width:150px' },
    h('div', { class: 'ink2' }, `실행 ${s.requests_running ?? 0} · 대기 ${s.requests_waiting ?? 0}`),
    h('div', { class: 'meter', title: `KV 캐시 ${fmt.pct(kv)}` }, h('i', { style: `width:${Math.min(100, kv * 100)}%` })),
    h('div', { class: 'muted' }, `KV 캐시 ${fmt.pct(kv)}`));
}

export async function render(root) {
  const providers = await api('GET', '/providers');
  const reload = () => render(root);
  const act = (fn) => () => guard(async () => { await fn(); await reload(); });

  const rows = providers.map((p) => h('tr', {},
    h('td', {}, h('strong', {}, p.name), h('div', { class: 'mono muted' }, p.base_url),
      h('span', { class: 'tag', title: '프롬프트 수집 정책' }, p.data_collection === 'allow' ? '수집 가능' : '수집 안 함'), p.quantization && h('span', { class: 'tag' }, p.quantization)),
    h('td', {}, p.kind),
    h('td', {}, p.enabled ? status(p.status) : status('off'), p.cooldown_s > 0 && h('div', { class: 'muted', title: '연속 실패로 일시 제외됨 (cooldown)' }, `쿨다운 ${p.cooldown_s}초`),
      p.last_error && h('div', { class: 'muted', title: p.last_error }, p.last_error.slice(0, 40))),
    h('td', { class: 'num' }, fmt.ms(p.latency_ms), h('div', { class: 'muted' }, fmt.ago(p.last_checked_at))),
    h('td', {}, engine(p)),
    h('td', { class: 'num' }, fmt.int(p.usage_24h.requests), h('div', { class: 'muted' }, fmt.compact(p.usage_24h.tokens) + ' tok')),
    h('td', { class: 'num' }, p.routes),
    h('td', {}, h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: act(async () => { await api('POST', `/providers/${p.id}/check`); toast('헬스체크 완료'); }) }, '점검'),
      h('button', { class: 'btn sm', onclick: () => guard(async () => {
        const r = await api('POST', `/providers/${p.id}/sync-models`);
        const missing = r.discovered.length;
        notice(`${p.name} 서빙 모델`, h('p', {}, missing ? r.discovered.join(', ') : '모델이 없습니다.'),
          missing && h('button', { class: 'btn primary', onclick: act(async () => { const x = await api('POST', `/providers/${p.id}/sync-models?register=true`); toast(`${x.registered.length}개 모델 등록`); document.querySelector('dialog')?.close(); }) }, '레지스트리에 등록'));
      }) }, '모델 동기화'),
      h('button', { class: 'btn sm', onclick: () => guard(async () => {
        const v = await formDialog('프로바이더 수정', fields(p));
        if (v) { await api('PATCH', `/providers/${p.id}`, body(v, true)); await reload(); }
      }) }, '수정'),
      h('button', { class: 'btn sm', onclick: act(() => api('PATCH', `/providers/${p.id}`, { enabled: !p.enabled })) }, p.enabled ? '끄기' : '켜기'),
      h('button', { class: 'btn sm danger', onclick: () => guard(async () => {
        if (await confirmAction(`'${p.name}' 프로바이더를 삭제할까요? 연결된 라우트도 함께 삭제됩니다.`)) { await api('DELETE', `/providers/${p.id}`); await reload(); }
      }) }, '삭제'))),
  ));

  mount(root, 
    h('div', { class: 'row between', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '프로바이더 (LLM 엔진)'),
      h('button', { class: 'btn primary', onclick: () => guard(async () => {
        const v = await formDialog('프로바이더 추가', fields());
        if (v) { const p = await api('POST', '/providers', body(v, false)); await api('POST', `/providers/${p.id}/check`); await reload(); }
      }) }, '+ 프로바이더 추가')),
    h('div', { class: 'card table-wrap' }, providers.length
      ? h('table', {}, h('thead', {}, h('tr', {}, ['이름 / URL', '종류', '상태', '지연', '엔진 부하', '24h 요청', '라우트', ''].map((t, i) => h('th', { class: i === 3 || i >= 5 && i < 7 ? 'num' : '' }, t)))), h('tbody', {}, rows))
      : h('p', { class: 'muted' }, 'vLLM, Ollama 같은 엔진을 추가하세요. (데모: python -m llmgate.demo)')));
}
