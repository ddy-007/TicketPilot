from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import ROOT_ENV, ConfigurationError, valid_token


class SimulatorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV, env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: str = "local"
    simulator_database_url: str = Field(min_length=1, repr=False)
    simulator_read_token: SecretStr
    simulator_write_token: SecretStr

    def require_tokens(self) -> None:
        read = self.simulator_read_token.get_secret_value()
        write = self.simulator_write_token.get_secret_value()

        if not valid_token(read) or not valid_token(write) or read == write:
            raise ConfigurationError(
                "Simulator tokens must be distinct, contain 32-256 printable ASCII "
                "characters without whitespace, and must not be example placeholders."
            )


def get_simulator_settings() -> SimulatorSettings:
    try:
        settings = SimulatorSettings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]).upper() for error in exc.errors()})
        raise ConfigurationError(
            "Missing or invalid simulator configuration: " + ", ".join(fields)
        ) from None
    settings.require_tokens()
    return settings
