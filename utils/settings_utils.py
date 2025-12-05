import os
from typing import Any, Dict, Optional
import requests

try:
    from utils import storage
except Exception:  # pragma: no cover
    storage = None

DEFAULT_BACKEND_URL = "https://spin-api-pba3.onrender.com"


def _sanitize_url(url: Optional[str], fallback: str) -> str:
    if url and url.strip():
        return url.strip().rstrip("/")
    return fallback


def _current_backend_url() -> str:
    if storage:
        backend = storage.get_backend_url()
        if backend:
            return backend.rstrip("/")
    env_value = os.getenv("BACKEND_URL")
    return _sanitize_url(env_value, DEFAULT_BACKEND_URL)


def _auth_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    token = storage.get_token() if storage else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def save_user_settings(
    phone: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    upi_id: Optional[str] = None,
) -> bool:
    """
    Patch the user's profile via /users/me to keep server + storage in sync.
    """
    backend = _current_backend_url()
    if not backend:
        print("[ERROR] save_user_settings: backend URL missing")
        return False

    payload: Dict[str, Any] = {}
    if phone:
        payload["phone"] = phone.strip()
    if name:
        payload["name"] = name.strip()
    if description:
        payload["description"] = description.strip()
    if upi_id:
        payload["upi_id"] = upi_id.strip()

    if not payload:
        print("[WARN] save_user_settings: nothing to update")
        return False

    try:
        resp = requests.patch(
            f"{backend}/users/me",
            json=payload,
            headers=_auth_headers(),
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200:
            if storage:
                try:
                    storage.set_user(resp.json())
                except Exception:
                    pass
            return True

        print(f"[WARN] save_user_settings failed: {resp.status_code} {resp.text}")
        return False
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"[ERROR] Failed to save settings: {exc}")
        return False
