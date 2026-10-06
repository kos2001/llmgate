# 개발 가이드

개발용으로 쓰는 가장 빠른 길은 `llmgate dev` 입니다. (운영 용도가 아닙니다: 고정 관리자 토큰·고정 세션 시크릿·mock 엔진.)

## 1. 설치와 실행

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                                   # 편집 가능 설치 + pytest + openai SDK
llmgate dev                                               # mock vLLM + SSO 자동 로그인 + 샘플 데이터 + 자동 재시작 + 영속 DB
```

처음 실행하면 이렇게 출력됩니다.

```
  llmgate (development mode)
    dashboard : http://127.0.0.1:8080/dashboard/   admin token: dev-admin
    API docs  : http://127.0.0.1:8080/docs   OpenAI base URL: http://127.0.0.1:8080/v1   OpenRouter-style: http://127.0.0.1:8080/api/v1
    mock vLLM : http://127.0.0.1:8101/v1   (models: dev-chat, dev-embed)
    SSO       : mock IdP http://127.0.0.1:8200  -> signed in automatically as alice (admin)
    seed data : 3 providers, 10 models, 6 services / 9 keys, 5 customers, 5 guardrails, 870 usage rows (14 days)
    API key   : sk-lg-…   (service 'dev'; every seeded key is in data\dev-credentials.json)
```

| 옵션 | 효과 |
|---|---|
| (기본) | **SSO 켜짐 + 자동 로그인**: 브라우저로 `/dashboard/` 를 열면 `dev-admin` 입력 없이 mock IdP 를 거쳐 **alice(관리자)** 로 바로 들어갑니다 (진짜 OIDC 코드+PKCE 흐름) |
| `--as bob` / `--as carol` | 다른 사용자(운영자/뷰어)로 자동 로그인. `--as none` 이면 사용자 선택 화면을 보여줍니다 |
| `--no-sso` | mock IdP 없이 시작. 관리자 토큰(`dev-admin`)으로 로그인 |
| `--no-mock` | mock vLLM 없이 시작. 대시보드 > 프로바이더에서 **진짜 vLLM**(`http://host:8000/v1`)을 추가 후 "모델 동기화" |
| `--no-seed` | 첫 실행에 샘플 데이터 대신 서비스 하나와 키만 만듭니다 |
| `--reset` | 개발 DB(`data/dev.db`)를 지우고 처음부터(샘플 데이터 다시 생성, 새 API 키) |
| `--no-reload` | 자동 재시작 끄기 |
| `--port` `--db` `--mock-port` `--idp-port` | 포트/DB 경로 변경 (여러 인스턴스를 나란히 띄울 때). 지정하지 않은 포트가 막혀 있으면 다음 빈 포트로 자동 이동 |

**샘플(시드) 데이터** — 첫 실행(빈 DB)에만 들어갑니다. 대시보드가 비어 있지 않도록:
프로바이더 3(mock vLLM 2대 + 꺼진 ollama), 모델 10(채팅·비전·추론·임베딩·리랭크, 가격·폴백·라우팅 전략·별칭), 서비스 6 / 키 9(예산·RPM·TPM·모델 허용 목록·만료·비활성), 고객 5(예산·차단), 가드레일 5, 사전 등록 사용자 2,
그리고 **14일치 사용 기록 약 870건**(업무 시간대 곡선, 오류·캐시 적중·폴백·가드레일 차단 포함). 모든 라우트는 mock 엔진이 실제로 서빙하는 모델을 가리켜서 시드된 키로 진짜 요청이 통과합니다.
모든 시드 키는 `data/dev-credentials.json` 에 있습니다. 개발 DB 가 아닌 곳에는 `LLMGATE_DATABASE_PATH=... llmgate seed [--reset] [--no-usage]` 로 넣을 수 있습니다.

동작 방식
- **자동 재시작**: `llmgate/` 의 `.py` 를 저장하면 서버가 다시 뜹니다. 세션 시크릿이 고정이라 SSO 로그인은 유지됩니다. 대시보드 JS/CSS 는 캐시 없이(ETag 재검증) 제공되므로 새로고침만 하면 됩니다.
- **DB 유지**: 두 번째 실행부터는 시드를 건드리지 않습니다(키·프로바이더·사용 로그 그대로). 개발 키는 DB 옆의 `<DB이름>-credentials.json`(기본 `data/dev-credentials.json`) 에 있습니다.
- Ctrl+C 로 종료하면 자식 프로세스(mock 엔진/IdP)도 함께 정리됩니다. 터미널을 강제로 닫아 mock 이 남았다면 8101/8200 포트를 쓰는 프로세스를 종료하세요.

## 서버가 안 뜰 때

| 증상 | 원인 / 해결 |
|---|---|
| `llmgate` 명령을 찾을 수 없음 | `pip install -e ".[dev]"` 를 했는지, 그 가상환경이 활성화돼 있는지 확인. PATH 와 무관하게 `python -m llmgate dev` 로도 실행됩니다 |
| `port … is not available` | 8080/8101/8200 을 다른 프로그램이 쓰거나 Windows(Hyper-V·Docker·WSL)가 예약한 경우. **지정하지 않았다면 다음 빈 포트로 자동 이동**하고 배너에 실제 주소를 보여줍니다. `--port 9000` 처럼 직접 지정한 포트가 막혀 있으면 오류로 종료합니다. 예약 범위는 `netsh interface ipv4 show excludedportrange protocol=tcp` |
| `uvicorn llmgate.main:app --reload` | 지원합니다(앱을 처음 접근할 때 만듭니다). 환경 변수는 `LLMGATE_*` |
| 시작하자마자 종료 / mock 이 안 뜸 | 콘솔에 자식 프로세스(mock vLLM·IdP)의 오류가 그대로 출력됩니다. `--no-mock` 으로 mock 없이, `--no-reload` 로 재시작 감시 없이 띄워 원인을 좁히세요 |
| 브라우저에서 접속 거부 | 터미널에 `Application startup complete` 가 나온 뒤에 접속하세요. 배너의 주소(포트가 바뀌었을 수 있음)를 확인 |
| 자동 로그인이 안 되고 로그인 화면이 뜸 | 로그아웃 직후에는 일부러 자동 로그인을 건너뜁니다. 쿠키가 차단됐다면 "자동 로그인에 실패했습니다" 라고 알려 줍니다(무한 이동 방지). 토큰은 `dev-admin` |
| 다른 사용자(뷰어/운영자)로 확인하고 싶음 | 로그아웃 → "다른 사용자로 로그인"(OIDC `prompt=select_account`), 또는 `llmgate dev --as carol` |

## 2. 호출해 보기

```bash
KEY=$(python -c "import json;print(json.load(open('data/dev-credentials.json'))['api_key'])")
curl localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model":"dev-chat","messages":[{"role":"user","content":"hello"}]}'
python examples/quickstart.py          # 공식 OpenAI SDK 로: 목록·채팅·스트리밍·라우팅 옵션
```

OpenAPI 문서는 `/docs`(Swagger), `/redoc`. 관리 API 는 `Authorization: Bearer dev-admin`.

mock vLLM 에는 장애를 일으키는 헤더가 있어 폴백/쿨다운을 손으로 확인할 수 있습니다(`mockvllm/server.py` 상단 설명):
`x-mock-status: 503`, `x-mock-delay: 0.5`, `x-mock-no-usage: 1`(엔진이 usage 를 안 줄 때의 추정 경로).

## 3. 테스트

```bash
python -m pytest                      # 전체 (약 80초: mock 엔진/IdP 를 실제 포트에 띄움)
python -m pytest tests/test_proxy.py -k stream
```

- `tests/conftest.py`: `gw`(기본 게이트웨이), `gw_factory(**settings)`(설정을 바꾼 게이트웨이), `mock_factory(models, token_delay=…, error_rate=…)`(mock vLLM), `idp`/`sso_gw`/`sso_login()`(SSO 흐름).
- 새 기능을 넣을 때는 `tests/test_routing_prefs.py` 의 `provider()/model()/key()/chat()` 헬퍼를 가져다 쓰면 몇 줄로 시나리오가 만들어집니다.
- Windows 에서는 시계 해상도가 거칩니다(~15 ms). 타임스탬프를 비교하는 테스트에는 `time.sleep(0.05)` 간격을 두세요.

## 4. 코드 지도

```
llmgate/main.py          앱 조립, CLI (serve · dev · import-litellm)
llmgate/proxy.py         요청 경로: 인증 → 한도 → 가드레일 → 캐시 → 계획 → 업스트림/폴백 → 기록
llmgate/routing.py       provider 옵션, 변형, 전략, auto 라우터      router.py / router_state.py: 후보 · 쿨다운
llmgate/streaming.py     응답 후처리(JSON/SSE), usage 정규화
llmgate/limits.py        RPM/TPM/동시성/예산(윈도우)                 usage.py: 기록과 집계
llmgate/rbac.py oidc.py jwtutil.py api_auth.py   인증·권한(SSO, JWT, 역할 정책 required_role)
llmgate/admin_*.py       /admin/api (대시보드용)   api_openai.py api_compat.py   클라이언트용 + OpenRouter 호환
llmgate/api_litellm_*.py LiteLLM 호환 관리 API     litellm_config.py   config.yaml 가져오기
llmgate/static/          대시보드(바닐라 JS, 빌드 없음: DOM 은 h() 로만 만들어 XSS 방지). 사이드바 카테고리는 app.js 의 NAV, 색은 style.css 의 [data-cat]
llmgate/seed.py seed_data.py   샘플 데이터(정의 / 삽입·사용 기록 생성)       dev.py   llmgate dev
tools/make_icons.py      favicon.svg 에서 ICO/PNG 아이콘 재생성(Chrome 필요)
mockvllm/ mockoidc/      개발·테스트용 엔진 / IdP
```

## 5. 자주 하는 작업

- **엔드포인트 추가**: `proxy.ENDPOINTS` 에 항목(+ `tokens.py`/`usageutil.py` 의 토큰 추정) → `api_openai` 가 `/v1`·`/api/v1` 양쪽에 자동 등록.
- **컬럼 추가**: `db.COLUMNS` 에 한 줄(기존 DB 도 `ALTER TABLE` 로 자동 마이그레이션).
- **권한 정책 변경**: `rbac.required_role()` 한 곳. 테스트 `test_rbac_jwt.py` 의 역할 매트릭스를 같이 고치세요.
- **환경 변수 추가**: `config.py` 의 `Settings` 필드(= `LLMGATE_<이름>`), `.env.example` 에 한 줄.

## 6. 알아둘 것

- 개발 모드 시크릿(`dev-admin`, 고정 JWT 시크릿)은 **공개된 값**입니다. `--host 0.0.0.0` 으로 열지 마세요.
- 속도/동시성/쿨다운 상태는 프로세스 메모리입니다: 자동 재시작 때 초기화됩니다.
