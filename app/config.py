from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str

    secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = ""

    # Relay-buffer retention (see app/core/retention.py). Configurable because
    # both numbers are policy judgment calls, not physical constants.
    relay_retention_days: int = 30
    active_device_window_days: int = 90

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
