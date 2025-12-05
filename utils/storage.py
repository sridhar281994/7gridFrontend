"""
In-memory session storage for the Kivy frontend.
Keeps tokens, user info, backend/wallet URLs, match metadata, etc.
"""

import os
from typing import Optional, Tuple, Dict, List, Any

DEFAULT_BACKEND_URL = "https://spin-api-pba3.onrender.com"
DEFAULT_WALLET_URL = "https://wallet.srtech.co.in"


def _sanitize_url(url: Optional[str], fallback: str) -> str:
    if url and url.strip():
        return url.strip().rstrip("/")
    return fallback


_state: Dict[str, Any] = {
    "token": None,
    "user": None,
    "current_match": None,
    "stake_amount": None,
    "player_names": (None, None, None),
    "player_ids": [None, None, None],
    "backend_url": _sanitize_url(os.getenv("BACKEND_URL"), DEFAULT_BACKEND_URL),
    "wallet_url": _sanitize_url(os.getenv("WALLET_WEB_URL"), DEFAULT_WALLET_URL),
    "my_player_index": None,
    "num_players": 2,
}

# ---- token handling ----
def set_token(token: Optional[str]):
    _state["token"] = token


def get_token() -> Optional[str]:
    return _state.get("token")


# ---- user info ----
def set_user(user: dict):
    """
    Save user info and normalize name field.
    If no proper name is returned from backend, fallback to email prefix or phone.
    """
    if not isinstance(user, dict):
        return

    name = (user.get("name") or "").strip()
    email = user.get("email")
    phone = user.get("phone")

    if name:
        user["display_name"] = name
    elif email and "@" in email:
        user["display_name"] = email.split("@", 1)[0]
    elif phone:
        user["display_name"] = phone
    else:
        user["display_name"] = "Player"

    _state["user"] = user


def get_user() -> Optional[dict]:
    return _state.get("user")


def get_display_name() -> str:
    user = _state.get("user") or {}
    return user.get("display_name") or user.get("name") or user.get("email") or "Player"


# ---- match handling ----
def set_current_match(match_id: Optional[int]):
    _state["current_match"] = match_id


def get_current_match() -> Optional[int]:
    return _state.get("current_match")


# ---- stake handling ----
def set_stake_amount(amount: Optional[int]):
    _state["stake_amount"] = amount


def get_stake_amount() -> Optional[int]:
    return _state.get("stake_amount")


# ---- player names ----
def set_player_names(p1: Optional[str], p2: Optional[str], p3: Optional[str] = None):
    _state["player_names"] = (p1, p2, p3)


def get_player_names() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    return _state.get("player_names", (None, None, None))


# ---- player ids ----
def set_player_ids(ids: List[Optional[int]]):
    if not isinstance(ids, list):
        ids = list(ids)
    while len(ids) < 3:
        ids.append(None)
    _state["player_ids"] = ids[:3]


def get_player_ids() -> List[Optional[int]]:
    val = _state.get("player_ids")
    if not isinstance(val, list):
        val = [None, None, None]
    while len(val) < 3:
        val.append(None)
    return val[:3]


# ---- number of players ----
def set_num_players(n: int):
    _state["num_players"] = n


def get_num_players() -> int:
    return _state.get("num_players", 2)


# ---- my player index ----
def set_my_player_index(idx: Optional[int]):
    _state["my_player_index"] = idx


def get_my_player_index() -> Optional[int]:
    return _state.get("my_player_index")


# ---- backend URL ----
def get_backend_url() -> str:
    return _state.get("backend_url") or DEFAULT_BACKEND_URL


def set_backend_url(url: Optional[str]):
    _state["backend_url"] = _sanitize_url(url, DEFAULT_BACKEND_URL)


# ---- wallet URL ----
def get_wallet_url() -> str:
    return _state.get("wallet_url") or DEFAULT_WALLET_URL


def set_wallet_url(url: Optional[str]):
    _state["wallet_url"] = _sanitize_url(url, DEFAULT_WALLET_URL)


# ---- clear all ----
def clear_all():
    """Reset all runtime state to defaults."""
    for key in list(_state.keys()):
        _state[key] = None
    _state["player_names"] = (None, None, None)
    _state["player_ids"] = [None, None, None]
    _state["backend_url"] = _sanitize_url(os.getenv("BACKEND_URL"), DEFAULT_BACKEND_URL)
    _state["wallet_url"] = _sanitize_url(os.getenv("WALLET_WEB_URL"), DEFAULT_WALLET_URL)
    _state["num_players"] = 2


# ----------------------------------------------------
# Stakes Cache (for ROBOTS Army detection)
# ----------------------------------------------------
_stakes_cache: List[Any] = []


def set_stakes_cache(stakes):
    global _stakes_cache
    _stakes_cache = stakes or []


def get_stakes_cache():
    return _stakes_cache
