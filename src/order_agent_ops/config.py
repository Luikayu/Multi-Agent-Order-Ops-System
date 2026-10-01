"""Application configuration loaded from the project YAML file."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class AppMetadata(BaseModel):
    """Metadata exposed by the application."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class FallbackModelSettings(BaseModel):
    """Optional secondary OpenAI-compatible endpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    base_url: str | None = None
    api_key_env: str | None = None
    model_name: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def require_enabled_endpoint(self) -> "FallbackModelSettings":
        if self.enabled and (not self.base_url or not self.model_name):
            raise ValueError(
                "Enabled model fallback requires base_url and model_name"
            )
        return self


class ModelSettings(BaseModel):
    """Model gateway settings shared by every agent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    model_name: str = Field(min_length=1)
    timeout_seconds: float = Field(default=10.0, gt=0)
    fallback: FallbackModelSettings = Field(
        default_factory=FallbackModelSettings
    )

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "ModelSettings":
        supported = {"mock", "openai_compatible"}
        if self.provider not in supported:
            raise ValueError(
                "Unsupported model provider; expected mock or openai_compatible"
            )
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible provider requires base_url")
        if self.provider == "mock" and self.fallback.enabled:
            raise ValueError("Model fallback requires openai_compatible provider")
        return self


class Settings(BaseModel):
    """Top-level application settings."""

    model_config = ConfigDict(extra="forbid")

    app: AppMetadata
    model: ModelSettings


MODEL_ENV_OVERRIDES = {
    "MODEL_PROVIDER": "provider",
    "MODEL_BASE_URL": "base_url",
    "MODEL_API_KEY_ENV": "api_key_env",
    "MODEL_NAME": "model_name",
    "MODEL_TIMEOUT_SECONDS": "timeout_seconds",
}

FALLBACK_MODEL_ENV_OVERRIDES = {
    "MODEL_FALLBACK_ENABLED": "enabled",
    "MODEL_FALLBACK_BASE_URL": "base_url",
    "MODEL_FALLBACK_API_KEY_ENV": "api_key_env",
    "MODEL_FALLBACK_NAME": "model_name",
    "MODEL_FALLBACK_TIMEOUT_SECONDS": "timeout_seconds",
}


def _apply_environment_overrides(raw_settings: dict[str, Any]) -> dict[str, Any]:
    """Apply the small, explicit set of stage 2 model overrides."""

    model_settings = raw_settings.setdefault("model", {})
    if not isinstance(model_settings, dict):
        raise ValueError("The 'model' settings section must contain a mapping")

    for environment_name, field_name in MODEL_ENV_OVERRIDES.items():
        if environment_name not in os.environ:
            continue

        value = os.environ[environment_name].strip()
        model_settings[field_name] = value or None

    fallback_settings = model_settings.setdefault("fallback", {})
    if not isinstance(fallback_settings, dict):
        raise ValueError("The 'model.fallback' settings section must contain a mapping")
    for environment_name, field_name in FALLBACK_MODEL_ENV_OVERRIDES.items():
        if environment_name not in os.environ:
            continue
        value = os.environ[environment_name].strip()
        fallback_settings[field_name] = value or None

    return raw_settings


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate settings from YAML."""

    settings_path = path or DEFAULT_SETTINGS_PATH
    with settings_path.open("r", encoding="utf-8") as settings_file:
        raw_settings: Any = yaml.safe_load(settings_file)

    if not isinstance(raw_settings, dict):
        raise ValueError(f"Settings file must contain a mapping: {settings_path}")

    return Settings.model_validate(_apply_environment_overrides(raw_settings))
