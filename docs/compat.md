# OpenRouter · LiteLLM 대응표

✅ 지원 ◐ 일부/다른 방식 ✖ 미지원. "mock 검증"은 mock vLLM 으로 테스트가 있다는 뜻입니다(실제 vLLM 과의 호환은 OpenAI 프로토콜 기준으로 설계).

## OpenRouter

| 기능 | | 비고 |
|---|---|---|
| OpenAI 호환 chat/completions/embeddings, SSE | ✅ | `/v1` 와 `/api/v1` 모두 |
| Responses API, Anthropic Messages, rerank | ✅ | 엔진이 제공할 때 그대로 중계, 방언별 usage 집계(Anthropic 의 input/output 분리 이벤트 포함) |
| `GET /api/v1/models` + 가격(토큰당 문자열)·컨텍스트·`supported_parameters`·모달리티 | ✅ | `?supported_parameters=tools`, `?input_modalities=image` 필터, `/models/count` |
| `GET /api/v1/models/{id}/endpoints` | ✅ | 프로바이더별 가격, 30분 가동률, 지연·처리량 p50/p75/p90/p99 |
| `GET /api/v1/providers`, `/key`, `/credits` | ✅ | credits = 가장 빡빡한 월 USD 예산 기준 |
| `GET /api/v1/generation?id=gen-…` | ✅ | 응답 `id` 가 곧 generation id. 서비스 간 격리 |
| 프로비저닝 키 API `/api/v1/keys` | ✅ | operator 이상, 생성 시 비밀 한 번만 반환 |
| `provider`: order · only · ignore · allow_fallbacks · sort · data_collection · quantizations · max_price · require_parameters | ✅ | 프로바이더에 `data_collection`·`quantization` 속성 |
| 모델 변형 `:nitro` `:floor` `:free` | ✅ | 별칭과도 조합(`gpt-4:floor`) |
| `models: [...]` 폴백, `openrouter/auto` | ✅ | `llmgate/auto`: 프롬프트 난이도(코드·수식·길이·tools)에 따라 값싼 모델/강한 모델 선택(가격을 성능 대용으로 쓰는 휴리스틱) + `plugins:[{id:"auto-router",allowed_models:[…]}]` |
| `transforms: ["middle-out"]` | ✅ | system·마지막 메시지는 유지하고 중간을 덜어냄(토큰은 추정치) |
| tools / `response_format` / 멀티모달 / reasoning 파라미터 | ◐ | 엔진으로 **그대로 전달**. 모델에 `supported_parameters`·모달리티를 적어 두면 `require_parameters`·이미지 입력 검사 |
| 앱 어트리뷰션 `HTTP-Referer` / `X-Title`, `user` | ✅ | 사용 로그·집계(`group_by=app|user`) |
| 사용량 방송(Broadcast) | ◐ | 일반 웹훅 + Prometheus. Langfuse/Datadog 등 전용 연동은 없음 |
| 웹검색 `:online` · 파일/PDF 파서 플러그인 | ✖ | |
| BYOK, 크레딧 결제·자동 충전, 이미지/오디오 생성, 모더레이션, Zero Completion Insurance | ✖ | 셀프호스팅 범위 밖 |
| 프롬프트 캐시(`cache_control`) | ◐ | 엔진으로 전달만 (비용 계산에 반영하지 않음) |

## LiteLLM 프록시

| 기능 | | 비고 |
|---|---|---|
| 가상 키 `/key/generate·update·delete·info·list·regenerate·block·unblock` | ✅ | `team_id`=서비스 이름, 사용자 지정 키 문자열, `duration`, `model_max_budget`, `metadata` |
| 팀 `/team/new·update·delete·info·list·block·unblock` | ◐ | 팀 = 서비스. 팀 멤버/조직 계층은 없음(사람은 SSO 사용자 + 역할) |
| 고객(최종 사용자) `/customer/*`, `user` 필드 예산/차단 | ✅ | |
| 예산 · `budget_duration` · RPM/TPM · `max_parallel_requests` · 모델별 예산 | ✅ | 키·서비스 양쪽. TPM 은 직전 60초 사용량 기준의 사후(soft) 한도 |
| 라우팅 전략 simple-shuffle · least-busy · latency · usage · cost | ✅ | 모델별 `routing_strategy` |
| 쿨다운(`allowed_fails`/`cooldown_time`), `num_retries`(요청별 포함) | ✅ | 단일 프로세스 메모리 |
| 폴백 · 컨텍스트 윈도우 폴백 · `model_group_alias` | ✅ | 모델 설정 / 별칭 테이블 |
| 배포별 rpm/tpm/max_parallel | ✅ | 라우트 설정 |
| 응답 캐시, `cache:{ttl,no-cache,no-store}`, `Cache-Control` | ◐ | 메모리 LRU, 비스트리밍만. Redis·시맨틱 캐시 ✖ |
| 가드레일: PII 마스킹, 시크릿 탐지, 금칙어, 인젝션 휴리스틱, 정규식, 길이 | ◐ | **정규식 기반**(Presidio 같은 ML 아님). 입력 전부·출력은 비스트리밍만 |
| 태그 기반 지출 `/spend/tags`, `/spend/logs`, `/global/spend/*`, `/global/activity` | ✅ | |
| `/model/info·new·update·delete`, `/v1/model/info` | ◐ | 자체 호스팅 `api_base` 필요(SaaS 엔드포인트 등록 거부) |
| `config.yaml` 가져오기 | ✅ | `llmgate import-litellm FILE [--dry-run]` · `POST /admin/api/import/litellm`; 환경 변수로만 바꿀 수 있는 항목은 `LLMGATE_*` 로 안내 |
| `/health`, `/health/liveliness`, `/health/readiness`, `/utils/token_counter` | ✅ | 토큰 수는 추정치 |
| Prometheus `/metrics` | ✅ | |
| Slack 알림 | ◐ | 프로바이더 다운/복구, 예산 80%·100% (Slack 호환 웹훅). 느린 응답·에러율 알림은 없음 |
| SSO · 관리자 UI · 감사 로그 | ✅ | LiteLLM 에서는 유료(Enterprise)인 영역 → [auth.md](auth.md) |
| Postgres/Redis, 100+ 프로바이더 변환, 배치/파일/어시스턴트/이미지/오디오 API, 패스스루 엔드포인트, 프롬프트 관리, Langfuse 등 콜백 | ✖ | |
