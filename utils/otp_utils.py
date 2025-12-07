import os
import time
from typing import Optional, Dict, Any
import requests

# === Backend base URL ===
BACKEND_BASE = os.getenv("BACKEND_BASE", "https://spin-api-pba3.onrender.com").rstrip("/")

# TLS verification:
# - Set OTP_VERIFY_SSL=true in env to enforce cert validation
# - Default is False because some laptops have corp/root-ca issues
VERIFY_SSL = os.getenv("OTP_VERIFY_SSL", "false").lower() == "true"
PASSWORD_RESET_PATH = os.getenv("PASSWORD_RESET_PATH", "/auth/reset-password")

# Networking timeouts / retries
TIMEOUT = float(os.getenv("OTP_HTTP_TIMEOUT", "40")) # per-request timeout
RETRIES = int(os.getenv("OTP_HTTP_RETRIES", "2")) # how many times to retry on failure


def _url(path: str) -> str:
    return f"{BACKEND_BASE}{path if path.startswith('/') else '/' + path}"


def _headers(token: Optional[str] = None) -> Dict[str, str]:
    hdrs = {"Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    return hdrs


def _extract_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "detail" in data:
                return str(data["detail"])
            if "message" in data:
                return str(data["message"])
            return str(data)
        return str(data)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _request(
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    token: Optional[str] = None,
    timeout: float = TIMEOUT,
) -> Dict[str, Any]:
    url = _url(path)
    attempts = 1 + max(0, RETRIES)
    last_exc: Optional[Exception] = None

    for i in range(attempts):
        try:
            resp = requests.request(
                method=method.upper(),
                url=url,
                json=json,
                params=params,
                headers=_headers(token),
                timeout=timeout,
                verify=VERIFY_SSL,
            )
            if not (200 <= resp.status_code < 300):
                msg = _extract_error(resp)
                http_err = requests.HTTPError(msg, response=resp)
                raise http_err

            try:
                return resp.json()
            except Exception:
                return {"ok": False, "raw": resp.text}

        except requests.ReadTimeout:
            last_exc = f"Timeout after {timeout}s (attempt {i+1}/{attempts})"
            if i < attempts - 1:
                time.sleep(1.5) # short wait before retry
                continue
            raise RuntimeError(f"Server cold start or too slow: {last_exc}")

        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(1.5)
                continue
            raise RuntimeError(f"Request failed after {attempts} attempts: {e}")

    if last_exc:
        raise RuntimeError(str(last_exc))
    raise RuntimeError("Unexpected request failure")


# --------------------
# OTP (phone -> email)
# --------------------
def send_otp(phone: str) -> Dict[str, Any]:
    payload = {"phone": str(phone).strip()}
    return _request("POST", "/auth/send-otp", json=payload)


def verify_otp(phone: str, otp: str) -> Dict[str, Any]:
    payload = {"phone": str(phone).strip(), "otp": str(otp).strip()}
    return _request("POST", "/auth/verify-otp", json=payload)


def send_otp_phone(phone: str) -> Dict[str, Any]:
    return send_otp(phone)


def verify_otp_phone(phone: str, otp: str) -> Dict[str, Any]:
    return verify_otp(phone, otp)


def reset_password(
    new_password: str,
    *,
    token: Optional[str] = None,
    phone: Optional[str] = None,
    otp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Reset account password after OTP verification.

    Args:
        new_password: Desired password value.
        token: Optional bearer token (if backend expects authenticated change).
        phone: Optional phone identifier (if backend requires it).
        otp: Optional OTP value (for backends that verify in the same request).
    """

    payload: Dict[str, Any] = {"password": new_password}
    if not token:
        if phone:
            payload["phone"] = str(phone).strip()
        if otp:
            payload["otp"] = str(otp).strip()
        if not (phone and otp):
            raise ValueError("reset_password requires either token or (phone + otp).")

    return _request("POST", PASSWORD_RESET_PATH, json=payload, token=token)


# -------------
# Registration
# -------------
def register_user(name: str,
                  phone: str,
                  email: str,
                  password: str,
                  upi_id: Optional[str] = None) -> Dict[str, Any]:
    body = {
        "name": name.strip(),
        "phone": phone.strip(),
        "email": email.strip(),
        "password": password,
    }
    if upi_id:
        body["upi_id"] = upi_id.strip()
    return _request("POST", "/auth/register", json=body)


# ---------
# Profile
# ---------
def get_profile(token: str) -> Dict[str, Any]:
    return _request("GET", "/users/me", token=token)


def update_profile(token: str,
                   name: Optional[str] = None,
                   upi_id: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, str] = {}
    if name is not None and name.strip():
        params["name"] = name.strip()
    if upi_id is not None and upi_id.strip():
        params["upi_id"] = upi_id.strip()
    return _request("POST", "/users/me/profile", params=params, token=token)


# ---------------------------------------------------
# Matchmaking helpers
# ---------------------------------------------------
def list_waiting_matches(token: str) -> Dict[str, Any]:
    return _request("GET", "/matches/list", token=token)


def create_or_wait_match(token: str, stake_amount: int) -> Dict[str, Any]:
    body = {"stake_amount": stake_amount}
    return _request("POST", "/matches/create", json=body, token=token)


def join_match(token: str, match_id: int) -> Dict[str, Any]:
    body = {"match_id": match_id}
    return _request("POST", "/matches/join", json=body, token=token)


def check_match_ready(token: str, match_id: int) -> Dict[str, Any]:
    return _request("GET", f"/matches/check?match_id={match_id}", token=token)


# ---------------------------------------------------
# Dice roll helper (NEW)
# ---------------------------------------------------
def roll_dice(token: str, match_id: int) -> Dict[str, Any]:
    """Roll a dice for this match (server ensures fairness & sync)."""
    body = {"match_id": int(match_id)}
    return _request("POST", "/matches/roll", json=body, token=token)


# ---------------------------------------------------
# Login OTP helpers (email/username + password)
# ---------------------------------------------------
def _login_identifier_payload(identifier: str) -> Dict[str, str]:
    ident = (identifier or "").strip()
    payload: Dict[str, str] = {"identifier": ident}
    if not ident:
        return payload
    if "@" in ident:
        payload["email"] = ident
    elif ident.isdigit():
        payload["phone"] = ident
    else:
        payload["username"] = ident
    return payload


class InvalidCredentialsError(PermissionError):
    """Raised when the backend reports wrong password/identifier."""


class LegacyOtpUnavailable(RuntimeError):
    """Raised when fallback OTP cannot run for non-phone identifiers."""


def request_login_otp(identifier: str, password: str) -> Dict[str, Any]:
    """
    Trigger an OTP for login after validating identifier + password.
    Falls back to legacy phone-only OTP if the backend does not expose
    the new email/password-based endpoint.
    """

    payload = _login_identifier_payload(identifier)
    payload["password"] = password

    try:
        return _request("POST", "/auth/login/request-otp", json=payload)
    except requests.HTTPError as err:
        status = getattr(err.response, "status_code", None)
        if status in (401, 403):
            raise InvalidCredentialsError("Incorrect password or identifier.") from err
        if status == 404:
            phone = payload.get("phone")
            if phone:
                return send_otp(phone)
            raise LegacyOtpUnavailable(
                "Secure OTP login requires using your registered phone number."
            ) from err
        raise


def verify_login_with_otp(identifier: str, password: str, otp: str) -> Dict[str, Any]:
    """
    Verify OTP-based login with identifier + password.
    Falls back to legacy phone OTP verification for backward compatibility.
    """

    payload = _login_identifier_payload(identifier)
    payload["password"] = password
    payload["otp"] = str(otp).strip()

    try:
        return _request("POST", "/auth/login/verify-otp", json=payload)
    except requests.HTTPError as err:
        status = getattr(err.response, "status_code", None)
        if status == 404:
            phone = payload.get("phone")
            if phone:
                return verify_otp(phone, otp)
        raise
    except Exception:
        raise
