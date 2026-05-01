"""Schwab OAuth token management for schwab-py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    from schwab import auth
    from schwab.client import Client as SchwabAPIClient
    SCHWAB_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without optional dependency
    auth = None
    SchwabAPIClient = None
    SCHWAB_AVAILABLE = False
    logger.warning("schwab-py not installed. Install with: pip install schwab-py")

from config.settings import settings


class AuthManager:
    def __init__(self, api_key: Optional[str] = None, app_secret: Optional[str] = None, token_path: Optional[Path] = None):
        if not SCHWAB_AVAILABLE:
            raise ImportError("schwab-py is required for Schwab API access. Install with: pip install schwab-py")

        self.api_key = api_key or settings.SCHWAB_API_KEY
        self.app_secret = app_secret or settings.SCHWAB_API_SECRET
        self.token_path = Path(token_path or settings.SCHWAB_TOKEN_PATH).expanduser()

        if not self.api_key or not self.app_secret:
            raise ValueError("Set SCHWAB_API_KEY and SCHWAB_API_SECRET in your environment or .env file.")

        logger.info(f"AuthManager initialized. Token path: {self.token_path}")

    def authenticate_interactive(self, redirect_uri: str = "https://127.0.0.1:8182") -> SchwabAPIClient:
        """Run Schwab's browser OAuth flow and save a token file."""
        assert auth is not None
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        client = auth.easy_client(
            api_key=self.api_key,
            app_secret=self.app_secret,
            callback_url=redirect_uri,
            token_path=str(self.token_path),
        )
        logger.info(f"Authentication successful; token saved to {self.token_path}")
        return client

    def get_client(self) -> SchwabAPIClient:
        """Create an authenticated schwab-py client from the saved token file."""
        if not self.token_path.exists():
            raise FileNotFoundError(
                f"Token file not found: {self.token_path}. Run authentication first."
            )
        assert auth is not None
        return auth.client_from_token_file(
            token_path=str(self.token_path),
            api_key=self.api_key,
            app_secret=self.app_secret,
        )

    def is_token_valid(self) -> bool:
        if not self.token_path.exists():
            return False
        try:
            token_data = json.loads(self.token_path.read_text())
            payload = _token_payload(token_data)
            if not payload.get("access_token") or not payload.get("refresh_token"):
                logger.warning("Token missing access_token or refresh_token")
                return False
            expires_at = _parse_expires_at(payload.get("expires_at"))
            if expires_at and expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
                logger.warning("Token is expired or expiring soon")
                return False
            return True
        except Exception as exc:
            logger.error(f"Error checking token validity: {exc}")
            return False

    def token_info(self) -> Optional[dict]:
        if not self.token_path.exists():
            return None
        try:
            token_data = json.loads(self.token_path.read_text())
            payload = _token_payload(token_data)
            expires_at = _parse_expires_at(payload.get("expires_at"))
            return {
                "exists": True,
                "valid": self.is_token_valid(),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "has_refresh_token": bool(payload.get("refresh_token")),
            }
        except Exception as exc:
            return {"exists": True, "valid": False, "error": str(exc)}


def _token_payload(token_data: dict) -> dict:
    """Support both raw OAuth payloads and schwab-py's {'token': {...}} format."""
    nested = token_data.get("token")
    return nested if isinstance(nested, dict) else token_data


def _parse_expires_at(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # schwab-py stores epoch seconds.
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


try:
    auth_manager = AuthManager()
except (ValueError, ImportError):
    auth_manager = None
