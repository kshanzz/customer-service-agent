from __future__ import annotations

import os
import secrets
from collections.abc import Sequence

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader


API_KEY_HEADER = "X-API-Key"
PLACEHOLDER_KEYS = {
    "replace-me",
    "replace-with-a-random-key",
    "your-api-key",
    "your-api-key-here",
    "change-me",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _validate_api_key(api_key: str | None) -> str:
    if not api_key or api_key.strip().lower() in PLACEHOLDER_KEYS or len(api_key) < 32:
        raise ValueError(
            "AGENT_API_KEY must be at least 32 characters and not a placeholder"
        )
    return api_key


def resolve_auth_config(
    *,
    auth_required: bool | None = None,
    api_key: str | None = None,
    docs_enabled: bool | None = None,
    cors_origins: str | Sequence[str] | None = None,
) -> tuple[bool, str | None, bool, list[str]]:
    """Resolve explicit settings first, then environment settings."""
    required = (
        auth_required
        if auth_required is not None
        else _env_bool("AGENT_AUTH_REQUIRED", False)
    )
    resolved_key = api_key if api_key is not None else os.getenv("AGENT_API_KEY")
    if required:
        resolved_key = _validate_api_key(resolved_key)

    resolved_docs = (
        docs_enabled
        if docs_enabled is not None
        else _env_bool("AGENT_DOCS_ENABLED", not required)
    )

    if cors_origins is None:
        raw_origins = os.getenv("AGENT_CORS_ORIGINS", "")
        origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    elif isinstance(cors_origins, str):
        origins = [origin.strip() for origin in cors_origins.split(",") if origin.strip()]
    else:
        origins = [origin.strip() for origin in cors_origins if origin.strip()]

    if required and "*" in origins:
        raise ValueError("AGENT_CORS_ORIGINS must not contain * when authentication is enabled")
    return required, resolved_key, resolved_docs, origins


def api_key_dependency(api_key: str | None, auth_required: bool):
    header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

    async def require_api_key(provided_key: str | None = Security(header)) -> None:
        if not auth_required:
            return
        if provided_key is None or api_key is None or not secrets.compare_digest(
            provided_key, api_key
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "APIKey"},
            )

    return require_api_key
