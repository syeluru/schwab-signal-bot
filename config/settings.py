"""Environment-driven settings for the Schwab signal bot.

Keep credentials in a local .env file or your shell environment. Do not commit
real Schwab app keys, account IDs, or token files.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    SCHWAB_API_KEY: str = ""
    SCHWAB_API_SECRET: str = ""
    SCHWAB_ACCOUNT_ID: str = ""
    SCHWAB_TOKEN_PATH: Path = Field(default_factory=lambda: Path.home() / ".schwab_token.json")

    # Safety default for public installs. Set PAPER_TRADING=False explicitly for live orders.
    PAPER_TRADING: bool = True

    SCHWAB_RATE_LIMIT_CALLS: int = 120
    SCHWAB_RATE_LIMIT_PERIOD: int = 60
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("SCHWAB_TOKEN_PATH", mode="before")
    @classmethod
    def expand_token_path(cls, value):
        if value in (None, ""):
            return Path.home() / ".schwab_token.json"
        return Path(str(value)).expanduser()

    def validate_schwab_credentials(self) -> bool:
        return bool(self.SCHWAB_API_KEY and self.SCHWAB_API_SECRET)


settings = Settings()
