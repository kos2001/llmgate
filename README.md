# llmgate

vLLM · Ollama · TGI 같은 **셀프호스팅 LLM 엔진 앞단에 두는 OpenRouter 스타일 게이트웨이**입니다.
모델 레지스트리, 서비스/API 키 관리, 토큰·비용 추적, 라우팅/폴백, SSO(OIDC)·JWT 인증과 역할 기반 권한을 한 서비스로 제공하고,
[OpenRouter](https://openrouter.ai) 와 [LiteLLM 프록시](https://github.com/BerriAI/litellm) 의 API 형태를 따라 기존 SDK/스크립트를 그대로 붙일 수 있습니다.
참고한 프로젝트: LiteLLM(가상 키·예산·라우터), new-api / one-api(채널·쿼터·대시보드), OpenRouter(카탈로그·`provider` 라우팅·`gen-` 아이디).

```
클라이언트 ──(OpenAI / OpenRouter / Anthropic 형식)──▶ llmgate ──▶ vLLM #1 · vLLM #2 · Ollama · ...
                                         │   키 인증 · 한도/예산 · 가드레일 · 캐시 · 라우팅 · 폴백
                                         └──▶ SQLite (사용량 로그, 레지스트리) · Prometheus /metrics · 웹훅
브라우저 ──(OIDC SSO)──▶ 대시보드 (개요 · 프로바이더 · 모델 · 서비스/키 · 제어 · 로그 · 플레이그라운드 · 사용자/권한)
```

> **vLLM은 mock으로 대체했습니다.** `mockvllm/` 이 vLLM의 OpenAI 호환 서버를 흉내 냅니다
> (`/v1/chat/completions` 스트리밍과 `usage`, `/v1/responses`, `/v1/messages`, `/v1/rerank`, `/health`, `/metrics`의 `vllm:*` 지표, 장애 주입).
> SSO 개발용 mock IdP(`mockoidc/`)도 있습니다. 실제 엔진은 프로바이더의 `base_url`만 바꾸면 됩니다.

## 빠른 시작 (개발용)

```bash
pip install -e ".[dev]"
llmgate dev                       # (또는 python -m llmgate dev) mock vLLM 2대 + SSO 자동 로그인 + 샘플 데이터 + 자동 재시작 + 영속 DB
llmgate dev --as carol            # 뷰어로 자동 로그인 (bob=운영자). --as none 이면 사용자 선택 화면
llmgate dev --no-sso              # SSO 없이: 관리자 토큰 dev-admin 으로 로그인
llmgate dev --no-mock             # 진짜 vLLM 을 쓸 때: 대시보드 > 프로바이더 추가(http://gpu:8000/v1) > 모델 동기화
```

- 대시보드 <http://127.0.0.1:8080/dashboard/> — 열면 **로그인 입력 없이 alice(관리자)로 바로 들어갑니다** (토큰 로그인은 `dev-admin`) · API 문서 `/docs` · 시드된 키는 모두 `data/dev-credentials.json`
- **샘플 데이터**가 첫 실행에 들어갑니다: 모델 10 · 서비스 6 / 키 9 · 가드레일 5 · 14일치 사용 기록 약 870건 (`--no-seed` 로 끔, 다른 DB 에는 `llmgate seed`)
- OpenAI SDK 로: `base_url="http://127.0.0.1:8080/v1"` (OpenRouter 식은 `/api/v1`), `python examples/quickstart.py`
- 포트가 이미 쓰이고 있으면 다음 빈 포트로 자동 이동하고 배너에 실제 주소를 보여줍니다. 그래도 안 뜨면 [서버가 안 뜰 때](docs/development.md#서버가-안-뜰-때).
- 코드를 저장하면 서버가 다시 뜨고 SSO 세션도 유지됩니다. 자세한 옵션·테스트·코드 지도는 **[docs/development.md](docs/development.md)**.
- 데이터가 가득 찬 둘러보기용 데모는 `python -m llmgate.demo` (매번 초기화).

> 개발 모드의 시크릿(`dev-admin` 등)은 공개된 값입니다. 운영에서는 아래 환경 변수로 직접 지정하세요.

```bash
LLMGATE_ADMIN_TOKEN=change-me llmgate --port 8080      # 운영 형태 실행 (자동 재시작·mock 없음)
```

LiteLLM을 쓰고 있었다면 `llmgate import-litellm config.yaml` 로 `model_list`/`router_settings` 를 가져옵니다.

## 무엇이 들어 있나

| 영역 | 기능 |
|---|---|
| **게이트웨이** | `/v1` 과 `/api/v1`(OpenRouter 경로) 양쪽: chat·completions·embeddings·**responses**·**messages**(Anthropic)·**rerank**, SSE 스트리밍, 스트림 `usage` 정확 집계(엔진이 안 주면 추정) |
| **레지스트리** | 프로바이더(vLLM/Ollama/TGI…, 헬스체크·`/metrics` 수집) · 모델(가격·컨텍스트·지원 파라미터·모달리티) · 라우트(우선순위·가중치·프로바이더별 단가·RPM/TPM/동시성) · 별칭 |
| **라우팅** | 요청별 `provider` 옵션(order·only·ignore·allow_fallbacks·sort·data_collection·quantizations·max_price·require_parameters), `:nitro` `:floor` `:free`, `models` 폴백 배열, `llmgate/auto`, 전략(weighted·least-busy·latency·cost·usage), 쿨다운, 재시도, 모델별 폴백/컨텍스트 폴백 |
| **키·예산** | 서비스(팀)·API 키·RPM/TPM/동시 요청·토큰/USD 예산·**예산 주기(1h/7d/30d)**·모델별 예산·허용 모델/프로바이더·만료·로테이션 · 최종 사용자(`user`) 예산/차단 · 태그/앱(`X-Title`) 지출 |
| **보호** | 가드레일(PII·시크릿·금칙어·정규식·프롬프트 인젝션·길이: block/mask, 입력+출력) · 응답 캐시 · `middle-out` 변환 |
| **인증·권한** | **OIDC SSO(Authorization Code + PKCE)**, 세션 JWT 쿠키, **IdP JWT**(JWKS 검증) 로 관리 API·추론 API 접근, **RBAC**(viewer/operator/admin), 클레임→역할 매핑, 사용자 관리·사전 등록, 감사 로그 → [docs/auth.md](docs/auth.md) |
| **관측** | 사용량 로그·집계·시계열, Prometheus `/metrics`, 사용량 웹훅(메타데이터만), Slack 호환 알림(프로바이더 다운, 예산 임계) |
| **호환 API** | OpenRouter: `/api/v1/models`(+필터)·`/models/{id}/endpoints`·`/providers`·`/key`·`/credits`·`/generation`·`/keys` · LiteLLM: `/key/*` `/team/*` `/customer/*` `/spend/*` `/global/spend/*` `/model/*` `/health*` `/utils/token_counter` → [docs/compat.md](docs/compat.md) |

## 설정 (환경 변수 `LLMGATE_*` 또는 `.env`)

전체 목록은 [`.env.example`](.env.example) 과 `llmgate/config.py`. 자주 쓰는 것:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ADMIN_TOKEN` | `admin-change-me` | 비상용 관리자 토큰(항상 admin). **반드시 변경.** `ADMIN_TOKEN_ENABLED=false` 로 끌 수 있음 |
| `DATABASE_PATH` | `data/llmgate.db` | SQLite 파일 |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | – | 설정하면 SSO 활성화 |
| `OIDC_ROLE_MAP`, `OIDC_ROLES_CLAIM`, `OIDC_DEFAULT_ROLE` | `llm-admins→admin` …, `roles`, `none` | IdP 그룹 → 역할. 매핑이 없으면 로그인 거부(`none`) |
| `JWT_SECRET` | 프로세스마다 랜덤 | 세션 JWT 서명키(≥32자). 워커가 여러 개거나 재시작 후에도 세션을 유지하려면 지정 |
| `CACHE_TTL_S` | `0` | >0 이면 동일한 비스트리밍 요청을 캐시 (요청별 `"cache"` 로도 제어) |
| `UPSTREAM_RETRIES`, `COOLDOWN_AFTER_FAILS`, `COOLDOWN_S` | `0`, `3`, `30` | 재시도 / 쿨다운 |
| `USAGE_WEBHOOK_URL`, `ALERT_WEBHOOK_URL` | – | 사용량 방송 / 알림 |
| `PUBLIC_CATALOG` | `true` | `/api/v1/models` 등을 키 없이 공개할지 |

## 테스트

```bash
python -m pytest        # 141개: 프록시·폴백·스트리밍 / SSO 흐름·RBAC·JWT 위조 / OpenRouter·LiteLLM 호환 / 가드레일·캐시·알림
```

테스트는 mock vLLM·mock IdP를 **실제 포트**에 띄워 게이트웨이와 진짜 네트워크로 통신합니다.

## 알려진 한계 (솔직하게)

- **업스트림은 OpenAI 호환 엔진**(또는 `messages`/`responses`/`rerank` 를 직접 제공하는 엔진)만 지원합니다. LiteLLM처럼 100여 개 SaaS 프로바이더 SDK 변환은 없습니다.
- 스트리밍 응답에는 **출력 가드레일을 적용할 수 없고**, 캐시도 비스트리밍 요청만 합니다.
- 토큰 추정(엔진이 `usage`를 주지 않을 때)은 근사치입니다(영문 ~4자/토큰, CJK 1자/토큰).
- RPM·동시성·쿨다운 상태는 **프로세스 메모리**에 있습니다(단일 인스턴스 기준). 다중 인스턴스에는 Redis 같은 공유 저장소가 필요합니다.
- 저장소는 SQLite 입니다(소규모·중규모 트래픽용). 이 환경에서는 Docker를 쓸 수 없어 `Dockerfile`/`docker-compose.yml` 은 **빌드해 보지 못했습니다.**
- OpenRouter의 웹검색(`:online`)·PDF 파서·BYOK·크레딧 결제·이미지 생성, LiteLLM의 Redis/시맨틱 캐시·Langfuse 콜백·배치/파일/어시스턴트 API 는 포함하지 않습니다. 자세한 대응표는 [docs/compat.md](docs/compat.md).
