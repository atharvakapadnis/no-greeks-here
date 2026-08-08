"""App configuration: env-driven, no credential defaults.

get_settings() builds a fresh Settings() on every call rather than caching
one at import time. This mirrors db.py reading DATABASE_PATH from the
environment lazily on every call, and it's what lets each test set its own
env vars (monkeypatch.setenv + a fresh tmp_path DB) before anything reads
them — nothing here is ever constructed at module-import time. A missing
SECRET_KEY or DATABASE_PATH raises pydantic's ValidationError the moment
get_settings() is called; main.py calls it during startup so this fails
loudly there rather than silently on first request.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SECRET_KEY: str
    DATABASE_PATH: str
    OPERATOR_PASSWORD: str
    ENV: Literal["dev", "prod"] = "prod"
    COOKIE_SECURE: bool | None = None

    @property
    def cookie_secure(self) -> bool:
        """Explicit COOKIE_SECURE always wins; otherwise True unless ENV=dev."""
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.ENV != "dev"


def get_settings() -> Settings:
    return Settings()
