import { h, mount, fmt, api, icon } from './lib.js';
import { stackedColumns, rankBars } from './charts.js';

const PERIODS = [['24시간', 1, 'hour'], ['7일', 7, 'day'], ['30일', 30, 'day']];
let period = 1;
let timer = null;
const AUTO_KEY = 'llmgate_overview_auto';
const autoOn = () => { try { return localStorage.getItem(AUTO_KEY) === '1'; } catch { return false; } };

// Each metric has its own colour + icon; the colour tints the card edge, the icon chip and the corner accent.
function stat(label, value, sub, color, ico) {
  return h('div', { class: 'card stat', style: `--cat:var(--${color})` }, h('div', { class: 'l' }, h('i', {}, icon(ico)), label),
    h('div', { class: 'v' }, value), sub && h('div', { class: 's' }, sub));
}

function buckets(days, bucket) {
  const step = bucket === 'hour' ? 3600 : 86400, now = Math.floor(Date.now() / 1000);
  const end = Math.floor(now / step) * step, n = bucket === 'hour' ? 24 : Math.ceil(days);
  return Array.from({ length: n }, (_, i) => end - (n - 1 - i) * step);
}

export async function render(root) {
  clearTimeout(timer);
  const [, days, bucket] = PERIODS.find((p) => p[1] === period);
  const [ov, series] = await Promise.all([
    api('GET', `/overview?days=${days}`),
    api('GET', `/usage/timeseries?days=${days}&bucket=${bucket}`),
  ]);
  const t = ov.totals, c = ov.counts;
  const byKey = new Map(series.map((r) => [r.bucket, r]));
  const rows = buckets(days, bucket).map((ts) => {
    const d = new Date(ts * 1000);
    const key = d.toISOString().slice(0, bucket === 'hour' ? 13 : 10);
    const r = [...byKey.entries()].find(([k]) => k.startsWith(key))?.[1];
    return {
      label: bucket === 'hour' ? `${String(d.getHours()).padStart(2, '0')}시` : `${d.getMonth() + 1}/${d.getDate()}`,
      prompt_tokens: r?.prompt_tokens || 0, completion_tokens: r?.completion_tokens || 0,
      extra: r ? [`요청 ${fmt.int(r.requests)}건 · 비용 ${fmt.usd(r.cost_usd)}`] : [],
    };
  });
  const okRate = t.requests ? 1 - t.errors / t.requests : 1;

  mount(root, 
    h('div', { class: 'row between', style: 'margin-bottom:16px' }, h('h2', { style: 'margin:0' }, '개요'),
      h('div', { class: 'row' },
        h('label', { class: 'row ink2', title: '10초마다 숫자와 차트를 갱신합니다' }, h('input', { type: 'checkbox', checked: autoOn(), onchange: (e) => {
          try { localStorage.setItem(AUTO_KEY, e.target.checked ? '1' : '0'); } catch { /* ignore */ }
          render(root);
        } }), '자동 새로고침'),
        PERIODS.map(([label, d]) => h('button', { class: 'btn sm keep' + (d === period ? ' primary' : ''), onclick: () => { period = d; render(root); } }, label)))),
    h('div', { class: 'grid' },
      stat('요청 수', fmt.int(t.requests), `성공률 ${fmt.pct(okRate)}`, 'blue', 'requests'),
      stat('총 토큰', fmt.compact(t.total_tokens), `입력 ${fmt.compact(t.prompt_tokens)} · 출력 ${fmt.compact(t.completion_tokens)}`, 'violet', 'tokens'),
      stat('비용', fmt.usd(t.cost_usd), '등록한 모델 단가 기준', 'green', 'cost'),
      stat('평균 지연', fmt.ms(t.avg_latency_ms), `TTFT ${fmt.ms(t.avg_ttft_ms)}`, 'orange', 'latency'),
      stat('캐시 적중', fmt.int(t.cache_hits), t.requests ? `전체 요청의 ${fmt.pct(t.cache_hits / t.requests)}` : '–', 'magenta', 'cache'),
      stat('프로바이더', `${c.providers_healthy}/${c.providers}`, `정상 · 모델 ${c.models} · 서비스 ${c.services} · 키 ${c.keys}`, 'aqua', 'health')),
    h('div', { class: 'card', style: 'margin-bottom:16px' }, h('h3', {}, `토큰 사용량 (${PERIODS.find((p) => p[1] === period)[0]})`),
      stackedColumns(rows, [{ key: 'prompt_tokens', label: '입력 토큰', color: 'var(--series-1)' }, { key: 'completion_tokens', label: '출력 토큰', color: 'var(--series-2)' }])),
    h('div', { class: 'grid wide' },
      h('div', { class: 'card' }, h('h3', {}, '모델별 토큰 Top 5'),
        rankBars(ov.top_models, { tip: (m) => [m.name, `요청 ${fmt.int(m.requests)}`, `토큰 ${fmt.int(m.total_tokens)}`, `비용 ${fmt.usd(m.cost_usd)}`] })),
      h('div', { class: 'card' }, h('h3', {}, '서비스별 토큰 Top 5'),
        rankBars(ov.top_services, { tip: (m) => [m.name, `요청 ${fmt.int(m.requests)}`, `토큰 ${fmt.int(m.total_tokens)}`, `비용 ${fmt.usd(m.cost_usd)}`] }))));
  if (autoOn()) timer = setTimeout(() => { if (root.isConnected) render(root).catch(() => {}); }, 10000);  // stops by itself once the page is left
}

