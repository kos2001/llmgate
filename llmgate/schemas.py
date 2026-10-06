from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ProviderKind = Literal["vllm", "ollama", "tgi", "openai", "other"]
ModelKind = Literal["chat", "embedding", "rerank"]
Param = Literal["tools", "tool_choice", "response_format", "structured_outputs", "reasoning", "logprobs", "seed", "stop"]
Modality = Literal["text", "image", "audio", "file"]
Strategy = Literal["weighted", "least-busy", "latency", "cost", "usage"]


def _check_url(v: str) -> str:
    v = v.strip().rstrip("/")
    if not v.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return v


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: ProviderKind = "vllm"
    base_url: str = Field(description="OpenAI-compatible root, e.g. http://gpu-1:8000/v1")
    api_key: str | None = None
    enabled: bool = True
    timeout_s: float | None = Field(default=None, gt=0)
    data_collection: Literal["allow", "deny"] = "deny"
    quantization: str | None = Field(default=None, max_length=20)

    _url = field_validator("base_url")(_check_url)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: ProviderKind | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    data_collection: Literal["allow", "deny"] | None = None
    quantization: str | None = Field(default=None, max_length=20)

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v) if v is not None else v


class RouteCreate(BaseModel):
    provider_id: int
    upstream_model: str = Field(min_length=1)
    priority: int = 0
    weight: int = Field(default=1, ge=1, le=1000)
    enabled: bool = True
    input_price_per_m: float | None = Field(default=None, ge=0, description="Overrides the model price for this provider")
    output_price_per_m: float | None = Field(default=None, ge=0)
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)


class RouteUpdate(BaseModel):
    upstream_model: str | None = Field(default=None, min_length=1)
    priority: int | None = None
    weight: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None
    input_price_per_m: float | None = Field(default=None, ge=0)
    output_price_per_m: float | None = Field(default=None, ge=0)
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)


class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    display_name: str | None = None
    description: str | None = None
    kind: ModelKind = "chat"
    context_length: int | None = Field(default=None, gt=0)
    input_price_per_m: float = Field(default=0, ge=0, description="USD per 1M prompt tokens")
    output_price_per_m: float = Field(default=0, ge=0, description="USD per 1M completion tokens")
    enabled: bool = True
    supported_parameters: list[Param] | None = Field(default=None, description="None = unknown (assume everything)")
    input_modalities: list[Modality] = ["text"]
    output_modalities: list[Modality] = ["text"]
    max_completion_tokens: int | None = Field(default=None, gt=0)
    routing_strategy: Strategy = "weighted"
    fallbacks: list[str] = Field(default=[], description="Models tried after this one fails")
    context_window_fallbacks: list[str] = Field(default=[], description="Tried after a context-length error")
    routes: list[RouteCreate] = []


class ModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    display_name: str | None = None
    description: str | None = None
    kind: ModelKind | None = None
    context_length: int | None = Field(default=None, gt=0)
    input_price_per_m: float | None = Field(default=None, ge=0)
    output_price_per_m: float | None = Field(default=None, ge=0)
    enabled: bool | None = None
    supported_parameters: list[Param] | None = None
    input_modalities: list[Modality] | None = None
    output_modalities: list[Modality] | None = None
    max_completion_tokens: int | None = Field(default=None, gt=0)
    routing_strategy: Strategy | None = None
    fallbacks: list[str] | None = None
    context_window_fallbacks: list[str] | None = None


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    enabled: bool = True
    monthly_token_limit: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = Field(default=None, description="Budget window, e.g. '1h', '7d', '30d'; unset = calendar month")
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    allowed_models: list[str] | None = Field(default=None, description="Model access for the whole service")
    metadata: dict | None = None


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    enabled: bool | None = None
    monthly_token_limit: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = Field(default=None, description="Budget window, e.g. '1h', '7d', '30d'; unset = calendar month")
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    allowed_models: list[str] | None = Field(default=None, description="Model access for the whole service")
    metadata: dict | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    allowed_models: list[str] = Field(default=[], description="Empty list = all models")
    allowed_providers: list[str] = Field(default=[], description="Provider names; empty list = all providers")
    rpm_limit: int | None = Field(default=None, gt=0)
    monthly_token_limit: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)
    model_budgets: dict[str, float] | None = Field(default=None, description="{model: USD per budget window}")
    metadata: dict | None = None
    expires_at: float | None = Field(default=None, description="Unix timestamp")


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    allowed_models: list[str] | None = None
    allowed_providers: list[str] | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    monthly_token_limit: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)
    model_budgets: dict[str, float] | None = Field(default=None, description="{model: USD per budget window}")
    metadata: dict | None = None
    expires_at: float | None = None


class CustomerIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    alias: str | None = None
    max_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    blocked: bool = False


class CustomerUpdate(BaseModel):
    alias: str | None = None
    max_budget_usd: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    blocked: bool | None = None


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=200)
    model_name: str = Field(min_length=1)


GuardrailKind = Literal["pii", "secrets", "keywords", "regex", "injection", "max_chars"]


class GuardrailIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: GuardrailKind
    mode: Literal["block", "mask"] = "block"
    apply_to: Literal["input", "output", "both"] = "input"
    config: dict = {}
    enabled: bool = True
    default_on: bool = True


class GuardrailUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    mode: Literal["block", "mask"] | None = None
    apply_to: Literal["input", "output", "both"] | None = None
    config: dict | None = None
    enabled: bool | None = None
    default_on: bool | None = None
