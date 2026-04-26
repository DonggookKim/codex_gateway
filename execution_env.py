from __future__ import annotations

from typing import Iterable


OPENAI_ENV = "openai"
LOCAL_OLLAMA_ENV = "local_ollama"
LOCAL_ALIAS_PROFILES = {"qwen3-8b"}


def infer_execution_env(
    model_profile: str | None,
    *,
    local_model_profiles: Iterable[str] = (),
) -> str:
    cleaned = (model_profile or "").strip()
    if not cleaned:
        return OPENAI_ENV

    normalized_local_models = {
        profile.strip()
        for profile in local_model_profiles
        if profile and profile.strip()
    }
    if cleaned in LOCAL_ALIAS_PROFILES:
        return LOCAL_OLLAMA_ENV
    if cleaned in normalized_local_models:
        return LOCAL_OLLAMA_ENV
    if ":" in cleaned and not cleaned.startswith("gpt-"):
        return LOCAL_OLLAMA_ENV
    return OPENAI_ENV
