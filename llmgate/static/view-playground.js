import { h, mount, fmt, toast } from './lib.js';

// Calls the public /v1 API exactly like a client would, so usage shows up in the logs.
export async function render(root) {
  const keyIn = h('input', { type: 'password', placeholder: 'sk-lg-… (서비스 · 키 탭에서 발급)', style: 'flex:1;min-width:200px' });
  const modelSel = h('select', {}, h('option', { value: '' }, '모델 선택'));
  const sortSel = h('select', { title: 'provider.sort', 'aria-label': '프로바이더 정렬' }, [['', '정렬: 기본'], ['price', '정렬: 가격'], ['latency', '정렬: 지연'], ['throughput', '정렬: 처리량']].map(([v, l]) => h('option', { value: v }, l)));
  const cacheBox = h('input', { type: 'checkbox' });
  const prompt = h('textarea', { rows: 3, placeholder: '메시지를 입력하세요', style: 'width:100%' });
  const log = h('div', { class: 'chat' });
  const meta = h('div', { class: 'muted' });
  let busy = false;

  const headers = () => ({ Authorization: 'Bearer ' + keyIn.value.trim(), 'Content-Type': 'application/json' });
  async function loadModels() {
    const res = await fetch('/v1/models', { headers: headers() });
    if (!res.ok) return toast('모델 목록을 불러오지 못했습니다: ' + (await res.json().catch(() => ({}))).error?.message);
    const { data } = await res.json();
    modelSel.replaceChildren(h('option', { value: 'llmgate/auto' }, 'llmgate/auto · 프롬프트에 맞춰 자동 선택'), ...data.filter((m) => m.architecture.modality === 'text->text').map((m) => h('option', { value: m.id }, `${m.id} · ${m.status}`)));
    if (!data.length) toast('이 키로 쓸 수 있는 모델이 없습니다.');
  }

  async function send() {
    if (busy || !prompt.value.trim() || !modelSel.value) return;
    busy = true;
    const user = h('div', { class: 'msg user' }, prompt.value), bot = h('div', { class: 'msg' }, '…');
    log.append(user, bot);
    const started = performance.now();
    let ttft = null, usage = null, text = '';
    try {
      const res = await fetch('/v1/chat/completions', { method: 'POST', headers: headers(), body: JSON.stringify({
        model: modelSel.value, stream: true, stream_options: { include_usage: true },
        ...(sortSel.value ? { provider: { sort: sortSel.value } } : {}), ...(cacheBox.checked ? { cache: { ttl: 300 } } : {}), messages: [{ role: 'user', content: prompt.value }] }) });
      prompt.value = '';
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || res.statusText);
      const provider = res.headers.get('x-llmgate-provider');
      const served = res.headers.get('x-llmgate-model');
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n'); buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data:') || line.includes('[DONE]')) continue;
          const obj = JSON.parse(line.slice(5));
          if (obj.usage) usage = obj.usage;
          const piece = obj.choices?.[0]?.delta?.content;
          if (piece) { ttft ??= performance.now() - started; text += piece; bot.textContent = text; }
        }
      }
      meta.textContent = `${served} @ ${provider} · TTFT ${fmt.ms(ttft)} · 총 ${fmt.ms(performance.now() - started)}` + (usage ? ` · 입력 ${usage.prompt_tokens} / 출력 ${usage.completion_tokens} 토큰 · ${fmt.usd(usage.cost)}` : '');
    } catch (e) { bot.textContent = '오류: ' + e.message; }
    busy = false;
    log.scrollTop = log.scrollHeight;
  }

  prompt.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send(); });
  mount(root, h('h2', {}, '플레이그라운드'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, keyIn, h('button', { class: 'btn keep', onclick: loadModels }, '모델 불러오기'), modelSel),
      h('div', { class: 'row', style: 'margin-top:8px' }, sortSel, h('label', { class: 'row ink2' }, cacheBox, '응답 캐시 사용 (5분)'), h('span', { class: 'hint' }, '스트리밍 요청은 캐시되지 않습니다.')),
      log, prompt, h('div', { class: 'row', style: 'margin-top:8px' }, meta, h('span', { class: 'spacer' }),
        h('button', { class: 'btn primary keep', onclick: send }, '보내기 (Ctrl+Enter)'))));
}
