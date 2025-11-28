"""
utils/storage.py
Handles simple in-memory storage for tokens, user info, match data, and player names/ids.
This is used by frontend screens to persist between navigation steps.
"""

from typing import Optional, Tuple, Dict, List, Any

# ---- session state (in-memory only) ----
_state: Dict[str, Any] = {
    "token": None,
    "user": None,
    "current_match": None,
    "stake_amount": None,
    "player_names": (None, None, None),  # (p1, p2, p3)
    "player_ids": [None, None, None],    # aligned with player_names
    "backend_url": "https://spin-api-pba3.onrender.com",  # default backend
    "my_player_index": None,  # 0 = P1, 1 = P2, 2 = P3
    "num_players": 2,  # default to 2-player mode
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

    if not user.get("id"):
        for key in ("user_id", "player_id", "uid"):
            if user.get(key) not in (None, "", "null"):
                user["id"] = user[key]
                break

    name = user.get("name")
    email = user.get("email")
    phone = user.get("phone")

    # ✅ Always prioritize actual full name from registration
    if name and isinstance(name, str) and name.strip():
        user["display_name"] = name.strip()
    else:
        # fallback: derive a readable label (only if name missing)
        if email and "@" in email:
            user["display_name"] = email.split("@", 1)[0]
        elif phone:
            user["display_name"] = phone
        else:
            user["display_name"] = "Player"

    _state["user"] = user


def get_user() -> Optional[dict]:
    """Return full user dict, including display_name if available."""
    return _state.get("user")


def get_user_id():
    """Helper for fetching whichever identifier backend supplied."""
    user = _state.get("user") or {}
    for key in ("id", "user_id", "player_id", "uid"):
        val = user.get(key)
        if val not in (None, "", "null"):
            return val
    return None


def get_display_name() -> str:
    """Get a safe display name for UI screens."""
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
    """Save player names (supports 2-player or 3-player mode)."""
    _state["player_names"] = (p1, p2, p3)

def get_player_names() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Retrieve player names as (p1, p2, p3)."""
    return _state.get("player_names", (None, None, None))

# ---- player ids ----
def set_player_ids(ids: List[Optional[int]]):
    """
    Save player ids aligned with player_names: [p1_id, p2_id, p3_id].
    Use None for unknown. For bots, you can use negative ids (e.g. -1000).
    """
    if not isinstance(ids, list):
        ids = list(ids)
    while len(ids) < 3:
        ids.append(None)
    _state["player_ids"] = ids[:3]

def get_player_ids() -> List[Optional[int]]:
    """Return [p1_id, p2_id, p3_id]."""
    val = _state.get("player_ids")
    if not isinstance(val, list):
        return [None, None, None]
    while len(val) < 3:
        val.append(None)
    return val[:3]

# ---- number of players ----
def set_num_players(n: int):
    """Save number of players in this match (2 or 3)."""
    _state["num_players"] = n

def get_num_players() -> int:
    """Get the number of players (default 2)."""
    return _state.get("num_players", 2)

# ---- my player index ----
def set_my_player_index(idx: Optional[int]):
    """Save my index (0 = P1, 1 = P2, 2 = P3)."""
    _state["my_player_index"] = idx

def get_my_player_index() -> Optional[int]:
    """Retrieve my index."""
    return _state.get("my_player_index")

# ---- backend URL ----
def get_backend_url() -> str:
    return _state.get("backend_url")

def set_backend_url(url: str):
    _state["backend_url"] = url

# ---- clear all ----
def clear_all():
    """Reset all runtime state to defaults."""
    for k in list(_state.keys()):
        _state[k] = None
    _state["player_names"] = (None, None, None)
    _state["player_ids"] = [None, None, None]
    _state["backend_url"] = "https://spin-api-pba3.onrender.com"
    _state["my_player_index"] = None
    _state["num_players"] = 2
