import os
from typing import Any, Dict, Optional
import requests

DEFAULT_BACKEND_URL = "https://spin-api-pba3.onrender.com"


def _sanitize_url(url: Optional[str], fallback: str) -> str:
    if url and url.strip():
        return url.strip().rstrip("/")
    return fallback


def _current_backend_url() -> str:
    env_value = os.getenv("BACKEND_URL")
    return _sanitize_url(env_value, DEFAULT_BACKEND_URL)


def save_user_settings(phone: str, name: str, description: str, upi_id: str) -> bool:
    """
    Send the latest profile settings to the backend.
    Returns True on success, False otherwise.
    """
    try:
        payload: Dict[str, Any] = {
            "phone": phone,
            "name": name,
            "description": description,
            "upi_id": upi_id,
        }
        resp = requests.post(
            f"{_current_backend_url()}/save-settings/",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            return True

        print(f"[WARN] save_user_settings failed: {resp.status_code} {resp.text}")
        return False
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"[ERROR] Failed to save settings: {exc}")
        return False
