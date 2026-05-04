from __future__ import annotations


OPENAI_ENV = "openai"


def infer_execution_env(model_profile: str | None = None) -> str:
    del model_profile
    return OPENAI_ENV
