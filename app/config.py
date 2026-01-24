from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    api_base_url: str = Field("http://localhost:8081", alias="API_BASE_URL")
    download_dir: str = Field("downloads", alias="DOWNLOAD_DIR")
    max_concurrent_downloads: int = Field(2, alias="MAX_CONCURRENT_DOWNLOADS")
    force_document: bool = Field(False, alias="FORCE_DOCUMENT")
    cookies_file: Optional[str] = Field(None, alias="YTDLP_COOKIES_FILE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")


def get_settings() -> Settings:
    s = Settings()
    os.makedirs(s.download_dir, exist_ok=True)
    return s
