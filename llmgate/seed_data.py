"""What the seed data contains (the logic that inserts it lives in ``seed.py``).

Two mock vLLM engines back it: every route below points at a model its engine really serves, so the seeded
gateway answers real requests, not just displays rows. ``tests/test_seed.py`` enforces that invariant.
"""

from __future__ import annotations

LLAMA = "meta-llama/Llama-3.1-8B-Instruct"
QWEN7 = "Qwen/Qwen2.5-7B-Instruct"
QWEN72 = "Qwen/Qwen2.5-72B-Instruct"
MISTRAL = "mistralai/Mistral-7B-Instruct-v0.3"
DEEPSEEK = "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
QWEN_VL = "Qwen/Qwen2-VL-7B-Instruct"
BGE = "BAAI/bge-m3"
RERANK = "BAAI/bge-reranker-v2-m3"
DEV_CHAT, DEV_EMBED = "dev-chat", "dev-embed"

# the model names each mock engine is started with
ENGINE_A_MODELS = [LLAMA, QWEN7, MISTRAL, QWEN_VL, BGE, RERANK, DEV_CHAT, DEV_EMBED]
ENGINE_B_MODELS = [LLAMA, QWEN72, DEEPSEEK]

PROVIDERS = [  # name, kind, engine ("a"/"b"/None), enabled, quantization, data_collection
    ("vllm-a100-1", "vllm", "a", 1, "bf16", "deny"),
    ("vllm-a100-2", "vllm", "b", 1, "fp8", "deny"),
    ("ollama-edge", "ollama", None, 0, "q4_k_m", "deny"),  # a planned node: registered but switched off
]

# name, kind, ctx, $in/1M, $out/1M, supported params, input modalities, max output, strategy, fallbacks, ctx-fallbacks,
# routes [(provider, upstream, priority, weight, route $in, route $out, rpm)], description
MODELS = [
    (LLAMA, "chat", 131072, 0.05, 0.08, ["tools", "response_format", "stop", "seed"], ["text"], 8192, "weighted", [QWEN7], [],
     [("vllm-a100-1", LLAMA, 0, 3, None, None, None), ("vllm-a100-2", LLAMA, 0, 1, 0.04, 0.07, 300)], "Meta Llama 3.1 8B Instruct, balanced across two GPUs 3:1"),
    (QWEN7, "chat", 32768, 0.04, 0.06, ["tools", "stop"], ["text"], 4096, "weighted", [LLAMA], [LLAMA],
     [("vllm-a100-1", QWEN7, 0, 1, None, None, None)], "Qwen 2.5 7B, cheap default; falls back to Llama on context overflow"),
    (QWEN72, "chat", 32768, 0.40, 0.60, ["tools", "response_format", "reasoning"], ["text"], 8192, "latency", [LLAMA], [],
     [("vllm-a100-2", QWEN72, 0, 1, None, None, 60)], "Qwen 2.5 72B for hard prompts (latency-based routing)"),
    (MISTRAL, "chat", 32768, 0.03, 0.05, ["tools", "stop"], ["text"], 4096, "weighted", [QWEN7], [],
     [("vllm-a100-1", MISTRAL, 0, 1, None, None, None)], "Mistral 7B Instruct v0.3"),
    (DEEPSEEK, "chat", 65536, 0.20, 0.30, ["reasoning", "stop"], ["text"], 16384, "weighted", [QWEN72], [],
     [("vllm-a100-2", DEEPSEEK, 0, 1, None, None, None)], "DeepSeek R1 distilled into Qwen 32B (reasoning)"),
    (QWEN_VL, "chat", 32768, 0.08, 0.12, ["tools"], ["text", "image"], 4096, "weighted", [], [],
     [("vllm-a100-1", QWEN_VL, 0, 1, None, None, None)], "Qwen2-VL 7B, accepts images"),
    (BGE, "embedding", 8192, 0.01, 0.0, None, ["text"], None, "weighted", [], [],
     [("vllm-a100-1", BGE, 0, 1, None, None, None)], "BGE-M3 multilingual embeddings"),
    (RERANK, "rerank", 8192, 0.01, 0.0, None, ["text"], None, "weighted", [], [],
     [("vllm-a100-1", RERANK, 0, 1, None, None, None)], "BGE reranker v2 m3"),
    (DEV_CHAT, "chat", 8192, 0.05, 0.10, ["tools", "stop"], ["text"], 2048, "weighted", [], [],
     [("vllm-a100-1", DEV_CHAT, 0, 1, None, None, None)], "Tiny model for quick experiments (the quickstart example uses it)"),
    (DEV_EMBED, "embedding", 8192, 0.01, 0.0, None, ["text"], None, "weighted", [], [],
     [("vllm-a100-1", DEV_EMBED, 0, 1, None, None, None)], "Tiny embedding model for quick experiments"),
]

ALIASES = {"gpt-4": QWEN72, "gpt-4o-mini": LLAMA, "gpt-3.5-turbo": QWEN7, "text-embedding-3-small": BGE}
CHAT_MODELS = [m[0] for m in MODELS if m[1] == "chat"]

# service: description, service-level limits, keys
# key: (name, allowed models or None, limits, status)  — limits: rpm, tpm, parallel, budget, budget_days, model_budgets, expires_in_days
SERVICES = [
    ("dev", "Created by llmgate dev: unrestricted", {}, [("dev-key", None, {}, "active")]),
    ("chatbot-web", "Customer-facing chat UI", {"budget": 50.0, "budget_days": 30, "rpm": 900}, [
        ("prod", [LLAMA, QWEN72, QWEN7], {"rpm": 600, "tpm": 2_000_000, "parallel": 32}, "active"),
        ("staging", [LLAMA], {"rpm": 60, "budget": 5.0}, "active")]),
    ("doc-summarizer", "Batch summarisation workers", {"tokens": 5_000_000}, [
        ("prod", [LLAMA, QWEN72, DEEPSEEK], {"rpm": 300, "model_budgets": {QWEN72: 20.0}}, "active")]),
    ("internal-search", "Embedding + rerank for the search team", {}, [("prod", [BGE, RERANK], {"rpm": 1200}, "active")]),
    ("data-science", "Notebooks and experiments", {"rpm": 120}, [
        ("notebook", [LLAMA, MISTRAL, DEEPSEEK, QWEN_VL], {"budget": 10.0, "budget_days": 7}, "active"),
        ("contractor", [LLAMA], {"rpm": 20, "expires_in_days": 3}, "active"),
        ("old-laptop", [LLAMA], {}, "disabled")]),
    ("ci-pipeline", "Nightly evals", {"rpm": 30}, [("nightly", [QWEN7, LLAMA], {"tpm": 200_000}, "active")]),
]

CUSTOMERS = [  # user_id, alias, budget, blocked
    ("user-ann", "Ann", 5.0, 0), ("user-bob", "Bob", None, 0), ("user-cho", "Cho", 2.0, 0),
    ("user-spam", "abusive account", None, 1), ("vip-dana", "Dana (VIP)", 100.0, 0),
]

GUARDRAILS = [  # name, kind, mode, apply_to, config, default_on
    ("mask-pii", "pii", "mask", "both", {"entities": ["EMAIL", "PHONE", "CREDIT_CARD", "KR_RRN"]}, 1),
    ("block-secrets", "secrets", "block", "input", {}, 1),
    ("block-injection", "injection", "block", "input", {}, 1),
    ("max-input", "max_chars", "block", "input", {"limit": 60000}, 1),
    ("internal-codenames", "keywords", "block", "input", {"words": ["Project-Falcon", "기밀프로젝트"]}, 0),  # opt-in per request
]

PENDING_USERS = [("erin@example.com", "admin"), ("frank@example.com", "viewer")]  # pre-provisioned; role applies at first SSO login
APPS = {"chatbot-web": "Chat UI", "doc-summarizer": "Doc Summarizer", "internal-search": "Search Bot", "data-science": "Jupyter",
        "ci-pipeline": "CI", "dev": "Quickstart"}
TAGS = [["prod"], ["prod"], ["prod", "eval"], ["staging"], ["experiment-7"], ["ci"], None, None]
END_USERS = ["user-ann", "user-bob", "user-cho", "vip-dana"]
