# 인증 · 권한 (SSO / OIDC / JWT / RBAC)

## 누가 무엇으로 들어오나

| 대상 | 방법 | 결과 |
|---|---|---|
| 사람 → 대시보드 | **OIDC SSO** (Authorization Code + **PKCE**, `state`, `nonce`) | 게이트웨이가 **세션 JWT**(HS256)를 `HttpOnly; SameSite=Lax` 쿠키로 발급 |
| 스크립트/CI → 관리 API | IdP가 발급한 **JWT**를 `Authorization: Bearer` 로 | JWKS로 서명 검증 → 역할은 클레임에서 결정 |
| 앱/CI → 추론 API (`/v1/*`) | API 키(`sk-lg-…`) **또는 IdP JWT** | JWT는 `llmgate_service` 클레임의 서비스에 묶인 가상 키로 처리 (한도·로그·허용 모델이 그대로 적용) |
| 비상용 | `LLMGATE_ADMIN_TOKEN` | 항상 `admin`. `ADMIN_TOKEN_ENABLED=false` 로 끌 수 있음 |

## IdP 설정 (Keycloak 예)

1. Client 생성: *Client authentication ON*, *Standard flow ON*, PKCE `S256`, Valid redirect URI `https://gw.example.com/auth/oidc/callback`.
2. 그룹/역할을 토큰의 `roles` 클레임으로 내보내는 Mapper 추가 (Keycloak은 *Group Membership* 또는 *User Realm Role* mapper, claim name `roles`).
3. API용 JWT를 받을 거라면 *Audience* mapper 로 `llmgate-api` 를 access token 의 `aud` 에 추가.

```bash
LLMGATE_OIDC_ISSUER=https://idp.example.com/realms/corp
LLMGATE_OIDC_CLIENT_ID=llmgate-dashboard
LLMGATE_OIDC_CLIENT_SECRET=...
LLMGATE_OIDC_AUDIENCE=llmgate-api                 # IdP JWT 를 bearer 로 받을 때 기대하는 aud (비우면 client id)
LLMGATE_OIDC_ROLES_CLAIM=roles                    # 점 표기 가능: realm_access.roles, groups
LLMGATE_OIDC_ROLE_MAP='{"llm-admins":"admin","llm-operators":"operator","llm-viewers":"viewer"}'
LLMGATE_OIDC_ALLOWED_EMAIL_DOMAINS=example.com
LLMGATE_JWT_SECRET=<32자 이상 랜덤>
LLMGATE_COOKIE_SECURE=true                        # HTTPS 뒤에서
```

다른 IdP: **Azure AD**는 App role 을 쓰면 `roles`, 그룹은 `groups`(GUID)로 매핑. **Okta**는 groups claim 을 추가.
**Google**은 그룹 클레임이 없으므로 `OIDC_ALLOWED_EMAIL_DOMAINS` + 사용자 **사전 등록** + `OIDC_DEFAULT_ROLE=viewer` 조합을 쓰세요.

## 역할과 권한

`viewer < operator < admin`. 정책은 `llmgate/rbac.py: required_role()` 한 곳에 있고 서버가 **모든 요청마다** 검사합니다(화면 숨김은 편의일 뿐).

| 역할 | 할 수 있는 일 |
|---|---|
| `viewer` | 대시보드·사용량·모델/서비스/프로바이더 조회, `/metrics`, 가드레일 시험기 |
| `operator` | + 모델·라우트·별칭·서비스·키·고객·가드레일·캐시 관리 (`/key/*` `/team/*` 포함) |
| `admin` | + **프로바이더**(업스트림 URL·자격 증명), **사용자·권한**, 감사 로그, 인증 설정, LiteLLM `config.yaml` 가져오기, `/model/*` |

- **IdP 그룹 → 역할**: 토큰의 여러 그룹 중 **가장 높은 역할**. 매핑이 없고 `OIDC_DEFAULT_ROLE=none`(기본값)이면 로그인 **거부**.
- **수동 지정이 우선**: 관리자가 대시보드에서 역할을 바꾸면 `role_source=manual` 이 되어 IdP 그룹이 바뀌어도 유지됩니다("IdP에 맡기기"로 되돌림).
- **사전 등록**: 이메일+역할을 미리 등록하면 첫 SSO 로그인 때 그 행에 연결됩니다(`email_verified` 가 false 면 연결 안 함).
- **즉시 폐기**: 사용자를 비활성화/삭제하면 이미 발급된 세션도 다음 요청에서 무효입니다(세션 JWT 에는 역할이 들어 있지 않고 매 요청 DB 를 조회).
- 자기 자신을 강등/비활성화/삭제할 수 없습니다(락아웃 방지).
- 세션 **쿠키**로 인증한 쓰기 요청(POST/PATCH/DELETE)에는 `x-llmgate-csrf` 헤더가 필수입니다(CSRF 방지). Bearer 인증은 해당 없음.

## 추론 API 에 JWT 쓰기

```bash
TOKEN=$(curl -s -u ci-pipeline:ci-secret -d grant_type=client_credentials https://idp.example.com/.../token | jq -r .access_token)
curl https://gw.example.com/v1/chat/completions -H "Authorization: Bearer $TOKEN" -d '{"model":"...","messages":[...]}'
```

- 토큰에 `llmgate_service`(이름은 `JWT_SERVICE_CLAIM`) 클레임이 있고 그 서비스가 **이미 등록**되어 있어야 합니다(서비스를 자동 생성하지 않음 → 권한 상승 방지).
- 선택 클레임 `llmgate_models` 는 허용 모델을 **좁히기만** 합니다(키/서비스 목록과 교집합).
- 처음 호출하면 `(issuer|sub)` 별 가상 키가 생겨 "서비스 · 키" 탭에 `JWT` 배지로 보입니다. 거기서 한도를 주거나 끄면(= 폐기) 즉시 반영됩니다.
- 게이트웨이가 발급한 **세션 JWT는 추론 자격이 아닙니다**(401).

## 보안 속성 (모두 테스트로 확인)

- 알고리즘 고정: IdP 토큰은 `RS256/ES256/PS256` 만. `alg: none`, HS\*(알고리즘 혼동), 다른 키로 서명한 토큰, 잘못된 `aud`/`iss`, 만료 토큰 → 401. 대칭 알고리즘은 설정으로도 허용되지 않음.
- 키 교체: 모르는 `kid` 를 만나면 JWKS 를 다시 가져옴(30초 간격 제한으로 IdP 폭주 방지).
- 디스커버리 `issuer` 가 설정과 다르면 거부.
- `state` 쿠키(서명, 10분) 불일치/재생/없음, 일회용 코드 → 로그인 거부. `next` 는 같은 사이트 상대 경로만(오픈 리디렉트 차단).
- 모든 관리 변경은 감사 로그(누가·언제·무엇을·결과).

## 하지 않는 것

리프레시 토큰, 백채널 로그아웃, SCIM, SAML. 로그아웃은 게이트웨이 세션만 끊고, IdP 의 `end_session_endpoint` 는 응답으로 알려 줄 뿐 자동 이동하지 않습니다.
