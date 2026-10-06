import { h, fmt, slot } from './lib.js';

const NS = 'http://www.w3.org/2000/svg';
const s = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...kids.filter((k) => k != null));
  return el;
};

export function showTip(evt, lines) {
  const tip = document.getElementById('tooltip');
  tip.replaceChildren(...lines.map((l, i) => h('div', { style: i === 0 ? 'font-weight:600;margin-bottom:2px' : '' }, l)));
  tip.hidden = false;
  const w = tip.offsetWidth, hgt = tip.offsetHeight;
  tip.style.left = Math.min(evt.clientX + 14, innerWidth - w - 8) + 'px';
  tip.style.top = Math.min(evt.clientY + 14, innerHeight - hgt - 8) + 'px';
}
export const hideTip = () => { document.getElementById('tooltip').hidden = true; };

/** Stacked column chart. rows: [{label, ...values}], series: [{key,label,color}] */
export function stackedColumns(rows, series, { height = 220, unit = fmt.compact } = {}) {
  const box = h('div', {});
  const legend = h('div', { class: 'legend' }, series.map((x) => h('span', {}, h('i', { style: `background:${x.color}` }), x.label)));
  const draw = () => {
    const W = Math.max(280, box.clientWidth || 600), pad = { l: 44, r: 8, t: 8, b: 22 };
    const iw = W - pad.l - pad.r, ih = height - pad.t - pad.b;
    const totals = rows.map((r) => series.reduce((a, x) => a + (r[x.key] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const svg = s('svg', { viewBox: `0 0 ${W} ${height}`, width: W, height, role: 'img', 'aria-label': '토큰 사용량 막대 차트' });
    for (let i = 0; i <= 5; i++) {
      const y = pad.t + ih - (ih * i) / 5;
      svg.append(s('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, stroke: i ? 'var(--grid)' : 'var(--axis)' }),
        s('text', { x: pad.l - 6, y: y + 4, 'text-anchor': 'end' }, unit((max * i) / 5)));
    }
    const slot = iw / Math.max(1, rows.length), bw = Math.min(40, slot * 0.7);
    const every = Math.ceil(rows.length / Math.max(1, Math.floor(iw / 56)));
    rows.forEach((r, i) => {
      const x = pad.l + slot * i + (slot - bw) / 2;
      let y = pad.t + ih;
      series.forEach((x2) => {
        const v = r[x2.key] || 0, hh = (v / max) * ih;
        if (hh > 0) { svg.append(s('rect', { x, y: y - hh, width: bw, height: Math.max(0, hh - 2), rx: 2, fill: x2.color })); }
        y -= hh;
      });
      if (i % every === 0) svg.append(s('text', { x: x + bw / 2, y: height - 6, 'text-anchor': 'middle' }, r.label));
      const hit = s('rect', { x: pad.l + slot * i, y: pad.t, width: slot, height: ih, fill: 'transparent' });
      hit.addEventListener('mousemove', (e) => showTip(e, [r.label, ...series.map((x2) => `${x2.label}: ${fmt.int(r[x2.key] || 0)}`), ...(r.extra || [])]));
      hit.addEventListener('mouseleave', hideTip);
      svg.append(hit);
    });
    box.replaceChildren(svg);
  };
  const wrap = h('div', {}, series.length > 1 && legend, box, tableView(rows, series));
  requestAnimationFrame(draw);
  new ResizeObserver(draw).observe(box);
  return wrap;
}

function tableView(rows, series) {
  return h('details', { style: 'margin-top:8px' }, h('summary', { class: 'muted' }, '표로 보기'),
    h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '구간'), series.map((x) => h('th', { class: 'num' }, x.label)))),
      h('tbody', {}, rows.map((r) => h('tr', {}, h('td', {}, r.label), series.map((x) => h('td', { class: 'num' }, fmt.int(r[x.key] || 0)))))))));
}

/** Horizontal ranking bars (single series, so no legend; the value is the direct label). */
export function rankBars(items, { value = (i) => i.total_tokens, label = (i) => i.name, format = fmt.compact, tip } = {}) {
  if (!items.length) return h('p', { class: 'muted' }, '아직 데이터가 없습니다.');
  const max = Math.max(1, ...items.map(value));
  return h('div', { style: 'display:grid;gap:8px' }, items.map((it) => {
    const row = h('div', { style: 'display:grid;grid-template-columns:minmax(80px,38%) 1fr auto;gap:8px;align-items:center' },
      h('span', { title: label(it), style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, h('i', { style: `display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:var(--c${slot(label(it))})` }), label(it)),
      h('div', { style: 'height:10px;background:var(--grid);border-radius:5px' },
        h('div', { style: `height:10px;border-radius:5px;background:var(--c${slot(label(it))});width:${Math.max(2, (value(it) / max) * 100)}%` })),
      h('span', { class: 'ink2', style: 'font-variant-numeric:tabular-nums' }, format(value(it))));
    row.addEventListener('mousemove', (e) => showTip(e, tip ? tip(it) : [label(it), format(value(it))]));
    row.addEventListener('mouseleave', hideTip);
    return row;
  }));
}

function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
