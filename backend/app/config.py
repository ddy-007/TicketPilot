from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class ConfigurationError(ValueError):
    """A safe configuration error that never includes input values."""


def valid_token(token: str) -> bool:
    """Return whether a configured local bearer token is safe to use."""
    return (
        32 <= len(token) <= 256
        and all(33 <= ord(char) <= 126 for char in token)
        and not token.lower().startswith("replace-with-")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = "local"
    app_database_url: str = Field(min_length=1, repr=False)
    checkpoint_database_url: str = Field(min_length=1, repr=False)
    llm_base_url: str = Field(default="", repr=False)
    llm_model: str = Field(default="", repr=False)
    llm_api_key: SecretStr = SecretStr("")
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    simulator_base_url: str = "http://127.0.0.1:8001"
    simulator_read_token: SecretStr = SecretStr("")
    max_model_calls: int = Field(default=6, ge=3, le=6)
    max_read_calls: int = Field(default=10, ge=3, le=10)
    simulator_write_token: SecretStr = SecretStr("")
    demo_operator_token: SecretStr = SecretStr("")
    demo_approver_token: SecretStr = SecretStr("")
    demo_tenant_id: str = "demo"

    def require_llm(self) -> None:
        fields = {
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_MODEL": self.llm_model,
            "LLM_API_KEY": self.llm_api_key.get_secret_value(),
        }
        missing = [name for name, value in fields.items() if not value.strip()]
        if missing:
            raise ConfigurationError("Missing model configuration: " + ", ".join(missing))
        try:
            parsed = urlparse(self.llm_base_url)
            valid = (
                parsed.scheme == "https"
                or parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
            if not valid or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            if parsed.query or parsed.fragment:
                raise ValueError
        except ValueError:
            raise ConfigurationError(
                "Invalid LLM_BASE_URL: use HTTPS (HTTP is allowed only on loopback), "
                "without credentials, query or fragment."
            ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors()})
        raise ConfigurationError("Missing or invalid configuration: " + ", ".join(names)) from None
