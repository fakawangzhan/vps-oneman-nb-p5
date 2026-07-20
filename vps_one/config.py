from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "VPS-ONE"
    secret_key: str = "change-me"
    master_key: str = "change-me-too"
    database_url: str = "sqlite+aiosqlite:///./data/vps-one.sqlite"
    base_url: str = "http://localhost:9080"
    debug: bool = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def settings() -> Settings:
    return Settings()
