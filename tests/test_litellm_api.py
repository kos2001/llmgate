from __future__ import annotations

import time

import pytest

from conftest import ADMIN, admin, bearer, sso_login
from test_routing_prefs import chat, key, model, provider

H = ADMIN


def post(gw, path, **body):
    r = gw.post(path, headers=H, json=body)
    return r


@pytest.fixture
def served(gw, mock_factory):
    srv = mock_factory(["mock-chat"])
    p = provider(gw, "alpha", srv)
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=100, output_price_per_m=100)
    return gw, srv


def use(gw, secret, **extra):
    return gw.post("/v1/chat/completions", headers=bearer(secret),
                   json={"model": "m", "messages": [{"role": "user", "content": "hi"}], **extra})


def test_key_lifecycle_in_litellm_shapes(served):
    gw, _ = served
    team = post(gw, "/team/new", team_alias="platform", max_budget=10, models=["m"], rpm_limit=100).json()
    assert team["team_id"] == "platform" and team["max_budget"] == 10 and team["models"] == ["m"]

    gen = post(gw, "/key/generate", key_alias="ci", team_id="platform", models=["m"], duration="30d", max_budget=1.0,
               budget_duration="7d", rpm_limit=60, tpm_limit=100000, max_parallel_requests=4,
               model_max_budget={"m": {"budget_limit": 0.5, "time_period": "1d"}}, metadata={"owner": "ml"})
    assert gen.status_code == 200, gen.text
    k = gen.json()
    assert k["key"].startswith("sk-lg-") and k["team_id"] == "platform" and k["key_alias"] == "ci"
    assert k["max_budget"] == 1.0 and k["budget_duration_s"] == 7 * 86400 and k["model_max_budget"] == {"m": 0.5}
    assert k["rpm_limit"] == 60 and k["max_parallel_requests"] == 4 and k["metadata"] == {"owner": "ml"}
    assert k["expires"] is not None and k["token"] == __import__("hashlib").sha256(k["key"].encode()).hexdigest()
    assert use(gw, k["key"]).status_code == 200

    info = gw.get("/key/info", headers=H, params={"key": k["token"]}).json()["info"]  # by hash or by plaintext
    assert info["spend"] > 0 and gw.get("/key/info", headers=H, params={"key": k["key"]}).json()["info"]["key_alias"] == "ci"
    own = gw.get("/key/info", headers=bearer(k["key"])).json()  # a virtual key reads itself, without admin rights
    assert own["info"]["key_alias"] == "ci"

    upd = post(gw, "/key/update", key=k["key"], max_budget=5.0, models=["m", "other"], rpm_limit=10).json()
    assert upd["max_budget"] == 5.0 and upd["models"] == ["m", "other"] and upd["rpm_limit"] == 10

    assert post(gw, "/key/block", key=k["key"]).json()["blocked"] is True and use(gw, k["key"]).status_code == 403
    assert post(gw, "/key/unblock", key=k["key"]).json()["blocked"] is False and use(gw, k["key"]).status_code == 200

    new = post(gw, "/key/regenerate", key=k["key"]).json()
    assert new["key"] != k["key"] and use(gw, k["key"]).status_code == 401 and use(gw, new["key"]).status_code == 200

    listed = gw.get("/key/list", headers=H, params={"team_id": "platform"}).json()
    assert listed["total_count"] == 1 and listed["keys"] == [new["token"]]
    full = gw.get("/key/list", headers=H, params={"return_full_object": True}).json()["keys"][0]
    assert full["key_alias"] == "ci"
    assert post(gw, "/key/delete", keys=[new["key"]]).json()["deleted_keys"] == [new["token"]]
    assert use(gw, new["key"]).status_code == 401


def test_a_virtual_key_cannot_read_another_key_or_use_admin_routes(served):
    gw, _ = served
    a = post(gw, "/key/generate", key_alias="a").json()
    b = post(gw, "/key/generate", key_alias="b").json()
    assert gw.get("/key/info", headers=bearer(a["key"]), params={"key": b["token"]}).status_code == 403
    assert gw.get("/key/list", headers=bearer(a["key"])).status_code == 401
    assert gw.post("/key/generate", headers=bearer(a["key"]), json={}).status_code == 401


def test_custom_key_and_validation(served):
    gw, _ = served
    custom = "sk-my-own-key-0123456789"
    assert post(gw, "/key/generate", key=custom).status_code == 200 and use(gw, custom).status_code == 200
    assert post(gw, "/key/generate", key=custom).status_code == 409  # unique
    assert post(gw, "/key/generate", team_id="ghost").status_code == 404
    assert post(gw, "/key/generate", duration="soon").status_code == 400
    assert post(gw, "/key/info").status_code in (404, 405)


def test_team_management(served):
    gw, _ = served
    post(gw, "/team/new", team_alias="alpha", max_budget=3, budget_duration="30d")
    post(gw, "/team/new", team_id="beta", team_alias="beta")
    assert post(gw, "/team/new", team_alias="alpha").status_code == 409
    assert {t["team_id"] for t in gw.get("/team/list", headers=H).json()} == {"alpha", "beta"}
    post(gw, "/key/generate", team_id="alpha", key_alias="k1")
    info = gw.get("/team/info", headers=H, params={"team_id": "alpha"}).json()
    assert info["team_info"]["budget_duration_s"] == 30 * 86400 and [k["key_alias"] for k in info["keys"]] == ["k1"]
    assert post(gw, "/team/update", team_id="alpha", max_budget=9, models=["m"]).json()["max_budget"] == 9
    assert post(gw, "/team/block", team_id="alpha").json()["blocked"] is True
    secret = post(gw, "/key/generate", team_id="alpha").json()["key"]
    assert use(gw, secret).json()["error"]["code"] == "service_disabled"
    assert post(gw, "/team/unblock", team_id="alpha").json()["blocked"] is False and use(gw, secret).status_code == 200
    assert post(gw, "/team/delete", team_ids=["beta"]).json() == {"deleted_teams": ["beta"]}


def test_customer_management(served):
    gw, _ = served
    secret = post(gw, "/key/generate").json()["key"]
    post(gw, "/customer/new", user_id="u1", alias="Una", max_budget=0.0001)
    assert use(gw, secret, user="u1").status_code == 200
    assert use(gw, secret, user="u1").json()["error"]["code"] == "customer_budget_exceeded"
    info = gw.get("/customer/info", headers=H, params={"end_user_id": "u1"}).json()
    assert info["spend"] > 0 and info["litellm_budget_table"]["max_budget"] == 0.0001 and info["alias"] == "Una"
    post(gw, "/customer/update", user_id="u1", max_budget=50)
    assert use(gw, secret, user="u1").status_code == 200
    post(gw, "/customer/block", user_ids=["u1"])
    assert use(gw, secret, user="u1").json()["error"]["code"] == "customer_blocked"
    post(gw, "/customer/unblock", user_ids=["u1"])
    assert use(gw, secret, user="u1").status_code == 200 and len(gw.get("/customer/list", headers=H).json()) == 1
    post(gw, "/customer/delete", user_ids=["u1"])
    assert gw.get("/customer/info", headers=H, params={"end_user_id": "u1"}).status_code == 404


def test_spend_reports(served):
    gw, _ = served
    k1 = post(gw, "/key/generate", key_alias="k1", team_id="t1").json() if post(gw, "/team/new", team_alias="t1").status_code == 200 else None
    use(gw, k1["key"], tags=["prod"], user="u9")
    use(gw, k1["key"], tags=["prod", "beta"])
    today = time.strftime("%Y-%m-%d", time.gmtime())

    logs = gw.get("/spend/logs", headers=H, params={"api_key": k1["token"]}).json()
    assert len(logs) == 2 and logs[0]["api_key"] == k1["token"] and logs[0]["spend"] > 0 and logs[0]["team_id"] == "t1"
    assert {l["user"] for l in logs} == {"u9", ""} and logs[0]["metadata"]["status"] == "success"
    one = gw.get("/spend/logs", headers=H, params={"request_id": logs[0]["request_id"]}).json()
    assert len(one) == 1 and gw.get("/spend/logs", headers=H, params={"user_id": "u9"}).json()[0]["user"] == "u9"
    assert len(gw.get("/spend/logs", headers=H, params={"start_date": today, "end_date": today}).json()) == 2
    assert gw.get("/spend/logs", headers=H, params={"start_date": "2001-01-01", "end_date": "2001-01-02"}).json() == []
    assert gw.get("/spend/logs", headers=H, params={"start_date": "yesterday"}).status_code == 400

    tags = {t["individual_request_tag"]: t for t in gw.get("/spend/tags", headers=H).json()}
    assert tags["prod"]["log_count"] == 2 and tags["beta"]["log_count"] == 1 and tags["prod"]["total_spend"] > tags["beta"]["total_spend"]
    assert gw.get("/global/spend/report", headers=H, params={"group_by": "team"}).json()[0]["group"] == "t1"
    assert gw.get("/global/spend/report", headers=H, params={"group_by": "customer"}).json()[0]["group"] in ("u9", "(none)")
    assert gw.get("/global/spend/report", headers=H, params={"group_by": "nope"}).status_code == 422
    assert gw.get("/global/spend/models", headers=H).json()[0]["model"] == "m"
    assert gw.get("/global/spend/keys", headers=H).json()[0]["key_alias"] == "k1"
    assert gw.get("/global/spend/logs", headers=H).json()[0]["date"] == today
    assert gw.get("/global/activity", headers=H).json()["sum_api_requests"] == 2


def test_model_deployments_api(served):
    gw, srv = served
    info = gw.get("/model/info", headers=H).json()["data"]
    assert info[0]["model_name"] == "m" and info[0]["litellm_params"]["model"] == "hosted_vllm/mock-chat"
    assert info[0]["model_info"]["input_cost_per_token"] == pytest.approx(100 / 1e6) and info[0]["model_info"]["mode"] == "chat"

    made = post(gw, "/model/new", model_name="gpt-4o-mini", litellm_params={"model": "hosted_vllm/mock-chat", "api_base": srv.base_url,
                                                                           "rpm": 30, "order": 2},
                model_info={"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002, "max_input_tokens": 4096})
    assert made.status_code == 200, made.text
    new_id = made.json()["model_id"]
    k = post(gw, "/key/generate").json()["key"]
    r = gw.post("/v1/chat/completions", headers=bearer(k), json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json()["usage"]["cost"] > 0
    registered = next(m for m in admin(gw, "GET", "/models") if m["name"] == "gpt-4o-mini")
    assert registered["input_price_per_m"] == pytest.approx(1.0) and registered["context_length"] == 4096
    assert registered["routes"][0]["rpm_limit"] == 30 and registered["routes"][0]["priority"] == 2
    assert len(admin(gw, "GET", "/providers")) == 1  # same api_base: the existing provider is reused

    assert post(gw, "/model/update", model_info={"id": new_id}, litellm_params={"rpm": 5, "order": 0}).status_code == 200
    assert gw.get("/model/info", headers=H, params={"litellm_model_id": new_id}).json()["data"][0]["litellm_params"]["rpm"] == 5
    assert post(gw, "/model/delete", id=new_id).status_code == 200 and post(gw, "/model/delete", id=new_id).status_code == 404
    assert post(gw, "/model/new", model_name="x", litellm_params={"model": "openai/x", "api_base": "ftp://nope"}).status_code == 400
    assert post(gw, "/model/new", model_name="x", litellm_params={"model": "openai/x"}).status_code == 400  # SaaS endpoints are out of scope


def test_health_and_utils(served):
    gw, srv = served
    assert gw.get("/health/liveliness").json() == "I'm alive!" and gw.get("/health/liveness").status_code == 200
    ready = gw.get("/health/readiness").json()
    assert ready["status"] == "healthy" and ready["db"] == "connected"
    assert gw.get("/health").status_code == 401
    h = gw.get("/health", headers=H, params={"live": True}).json()
    assert h["healthy_count"] == 1 and h["healthy_endpoints"][0]["model_name"] == "m"
    srv.stop()
    h = gw.get("/health", headers=H, params={"live": True}).json()
    assert h["unhealthy_count"] == 1 and h["unhealthy_endpoints"][0]["error"]

    tc = post(gw, "/utils/token_counter", model="m", messages=[{"role": "user", "content": "hello world"}]).json()
    assert tc["total_tokens"] > 3 and tc["tokenizer_type"] == "estimate"
    assert post(gw, "/utils/token_counter", text="안녕하세요").json()["total_tokens"] == 5


def test_litellm_routes_enforce_roles(sso_gw, idp):
    sso_login(sso_gw, idp, "bob")  # operator
    csrf = {"x-llmgate-csrf": "1"}
    assert sso_gw.post("/team/new", json={"team_alias": "x"}, headers=csrf).status_code == 200
    assert sso_gw.get("/team/list").status_code == 200
    r = sso_gw.post("/model/new", json={"model_name": "x", "litellm_params": {"model": "openai/x", "api_base": "http://h:1/v1"}}, headers=csrf)
    assert r.status_code == 403  # creating a provider needs admin
    assert sso_gw.post("/utils/token_counter", json={"text": "hi"}, headers=csrf).status_code == 200
    sso_login(sso_gw, idp, "carol")  # viewer
    assert sso_gw.get("/spend/tags").status_code == 200
    assert sso_gw.post("/key/generate", json={}, headers=csrf).status_code == 403
    assert sso_gw.post("/utils/token_counter", json={"text": "hi"}, headers=csrf).status_code == 200


# ------------------------------------------------------------------------------ config.yaml import

CONFIG = """
model_list:
  - model_name: llama-3.1-8b
    litellm_params:
      model: hosted_vllm/meta-llama/Llama-3.1-8B-Instruct
      api_base: {a}
      api_key: os.environ/UPSTREAM_KEY
      rpm: 600
      order: 1
    model_info:
      id: dep-1
      input_cost_per_token: 0.00000005
      output_cost_per_token: 0.00000008
      max_input_tokens: 131072
  - model_name: llama-3.1-8b
    litellm_params:
      model: hosted_vllm/meta-llama/Llama-3.1-8B-Instruct
      api_base: {b}
      order: 2
  - model_name: backup-model
    litellm_params: {{model: hosted_vllm/mock-chat, api_base: {b}}}
  - model_name: "gpt-*"
    litellm_params: {{model: openai/x, api_base: {a}}}
  - model_name: broken
    litellm_params: {{model: openai/x}}
router_settings:
  routing_strategy: latency-based-routing
  num_retries: 2
  cooldown_time: 15
  allowed_fails: 4
  fallbacks: [{{"llama-3.1-8b": ["backup-model"]}}, {{"nope": ["backup-model"]}}]
  model_group_alias: {{"gpt-4": "llama-3.1-8b"}}
litellm_settings:
  cache: true
  cache_params: {{ttl: 120}}
general_settings:
  master_key: sk-1234
"""


def test_import_litellm_config(gw, mock_factory, monkeypatch):
    a, b = mock_factory(["meta-llama/Llama-3.1-8B-Instruct", "mock-chat"]), mock_factory(["meta-llama/Llama-3.1-8B-Instruct", "mock-chat"])
    monkeypatch.setenv("UPSTREAM_KEY", "secret-from-env")
    text = CONFIG.format(a=a.base_url, b=b.base_url)

    preview = gw.post("/admin/api/import/litellm", headers=H, json={"yaml": text, "dry_run": True}).json()
    assert preview["dry_run"] and len(preview["deployments"]) == 3
    assert admin(gw, "GET", "/models") == [] and admin(gw, "GET", "/providers") == []  # a preview writes nothing

    report = gw.post("/admin/api/import/litellm", headers=H, json={"yaml": text}).json()
    assert [d["model"] for d in report["deployments"]] == ["llama-3.1-8b", "llama-3.1-8b", "backup-model"]
    assert report["strategy"] == "latency" and report["aliases"] == [{"alias": "gpt-4", "model": "llama-3.1-8b"}]
    assert report["fallbacks"]["llama-3.1-8b"]["fallbacks"] == ["backup-model"]
    assert any("wildcard" in w for w in report["warnings"]) and any("api_base" in w for w in report["warnings"])
    assert any("unknown model 'nope'" in w for w in report["warnings"])
    assert report["env"] == {"LLMGATE_ADMIN_TOKEN": "<your master_key>", "LLMGATE_CACHE_TTL_S": "120", "LLMGATE_UPSTREAM_RETRIES": "2",
                             "LLMGATE_COOLDOWN_S": "15", "LLMGATE_COOLDOWN_AFTER_FAILS": "4"}

    models = {m["name"]: m for m in admin(gw, "GET", "/models")}
    llama = models["llama-3.1-8b"]
    assert llama["routing_strategy"] == "latency" and llama["fallbacks"] == ["backup-model"] and len(llama["routes"]) == 2
    assert llama["input_price_per_m"] == pytest.approx(0.05) and llama["context_length"] == 131072
    assert [r["upstream_model"] for r in llama["routes"]] == ["meta-llama/Llama-3.1-8B-Instruct"] * 2
    assert llama["routes"][0]["rpm_limit"] == 600 and [r["priority"] for r in llama["routes"]] == [1, 2]
    providers = admin(gw, "GET", "/providers")
    assert len(providers) == 2 and any(p["has_api_key"] for p in providers)  # api_key came from os.environ/UPSTREAM_KEY

    k = post(gw, "/key/generate").json()["key"]
    r = gw.post("/v1/chat/completions", headers=bearer(k), json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["model"] == "llama-3.1-8b"  # migrated clients keep calling "gpt-4"
    again = gw.post("/admin/api/import/litellm", headers=H, json={"yaml": text}).json()
    assert len(admin(gw, "GET", "/providers")) == 2 and len(admin(gw, "GET", "/models")) == 2  # idempotent
    assert again["deployments"][0]["route_id"] == report["deployments"][0]["route_id"]


def test_import_rejects_bad_yaml_and_needs_admin(gw, sso_gw, idp):
    assert gw.post("/admin/api/import/litellm", headers=H, json={"yaml": "a: [unclosed"}).status_code == 400
    assert gw.post("/admin/api/import/litellm", headers=H, json={"yaml": "- just\n- a list"}).status_code == 400
    sso_login(sso_gw, idp, "bob")
    r = sso_gw.post("/admin/api/import/litellm", json={"yaml": "model_list: []"}, headers={"x-llmgate-csrf": "1"})
    assert r.status_code == 403
