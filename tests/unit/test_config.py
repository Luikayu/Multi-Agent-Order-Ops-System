from pathlib import Path

from order_agent_ops.config import load_settings


def _write_settings(path: Path) -> None:
    path.write_text(
        """
app:
  name: test-order-agent-ops
  version: 9.9.9
model:
  provider: mock
  base_url: null
  api_key_env: null
  model_name: yaml-model
  timeout_seconds: 10
  fallback:
    enabled: false
    base_url: null
    api_key_env: null
    model_name: null
    timeout_seconds: 10
""".strip(),
        encoding="utf-8",
    )


def test_load_settings_from_yaml(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    _write_settings(settings_path)

    settings = load_settings(settings_path)

    assert settings.app.name == "test-order-agent-ops"
    assert settings.model.provider == "mock"
    assert settings.model.base_url is None
    assert settings.model.model_name == "yaml-model"
    assert settings.model.timeout_seconds == 10
    assert settings.model.fallback.enabled is False


def test_environment_overrides_model_settings(tmp_path: Path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.yaml"
    _write_settings(settings_path)
    monkeypatch.setenv("MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("MODEL_API_KEY_ENV", "LOCAL_MODEL_API_KEY")
    monkeypatch.setenv("MODEL_NAME", "local-model")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("MODEL_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("MODEL_FALLBACK_BASE_URL", "https://cloud.example/v1")
    monkeypatch.setenv("MODEL_FALLBACK_API_KEY_ENV", "CLOUD_MODEL_API_KEY")
    monkeypatch.setenv("MODEL_FALLBACK_NAME", "cloud-model")
    monkeypatch.setenv("MODEL_FALLBACK_TIMEOUT_SECONDS", "30")

    settings = load_settings(settings_path)

    assert settings.model.provider == "openai_compatible"
    assert settings.model.base_url == "http://localhost:8000/v1"
    assert settings.model.api_key_env == "LOCAL_MODEL_API_KEY"
    assert settings.model.model_name == "local-model"
    assert settings.model.timeout_seconds == 25
    assert settings.model.fallback.enabled is True
    assert settings.model.fallback.base_url == "https://cloud.example/v1"
    assert settings.model.fallback.api_key_env == "CLOUD_MODEL_API_KEY"
    assert settings.model.fallback.model_name == "cloud-model"
    assert settings.model.fallback.timeout_seconds == 30
