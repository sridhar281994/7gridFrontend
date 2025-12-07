from threading import Thread
from typing import Optional, Dict, Any
from kivy.clock import Clock
from kivy.uix.screenmanager import Screen
from kivy.uix.popup import Popup
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.boxlayout import BoxLayout

# frontend helpers
from utils import storage
from utils.otp_utils import (
    request_login_otp,
    verify_login_with_otp,
    get_profile,
    InvalidCredentialsError,
    LegacyOtpUnavailable,
)


def _safe_text(screen: Screen, wid: str, default: str = "") -> str:
    """Read and strip TextInput text safely."""
    w = getattr(screen, "ids", {}).get(wid)
    return (w.text or "").strip() if w else default


def _popup(title: str, msg: str) -> None:
    """Thread-safe popup from worker threads that auto-closes after a delay."""

    def _open(*_):
        popup = Popup(
            title=title,
            content=Label(text=str(msg)),
            size_hint=(0.7, 0.3),
            auto_dismiss=True,
        )
        popup.open()

        # Auto-close after 2 seconds
        Clock.schedule_once(lambda dt: popup.dismiss(), 2)

    Clock.schedule_once(_open, 0)


class LoginScreen(Screen):
    # ---------- navigation ----------
    def go_back(self):
        if self.manager:
            self.manager.current = "welcome"

    def open_forgot_password(self) -> None:
        """Navigate to the forgot password flow, pre-filling the phone number."""
        if not self.manager:
            return

        phone = _safe_text(self, "phone_input")
        try:
            forgot_screen = self.manager.get_screen("forgot_password")
        except Exception:
            forgot_screen = None

        if forgot_screen and hasattr(forgot_screen, "prefill_phone"):
            forgot_screen.prefill_phone(phone)

        self.manager.current = "forgot_password"

    # ---------- UI actions (bound in KV) ----------
    def _read_identifier(self) -> str:
        return _safe_text(self, "phone_input")  # legacy id reused for email/username

    def _read_password(self) -> str:
        return _safe_text(self, "password_input")

    def _validate_identifier(self, identifier: str) -> bool:
        if not identifier:
            return False
        identifier = identifier.strip()
        if "@" in identifier and "." in identifier.split("@")[-1]:
            return True
        if identifier.isdigit() and 6 <= len(identifier) <= 15:
            return True
        return len(identifier) >= 3

    def send_otp_to_user(self) -> None:
        identifier = self._read_identifier()
        password = self._read_password()

        if not self._validate_identifier(identifier):
            _popup("Error", "Enter a valid registered email or username.")
            return
        if len(password) < 4:
            _popup("Error", "Enter your account password.")
            return

        def work():
            try:
                data: Dict[str, Any] = request_login_otp(identifier, password)
                ok = bool(data.get("ok", True))
                msg = data.get("message") or ("OTP sent successfully." if ok else "Failed to send OTP.")
                _popup("Success" if ok else "Error", msg)
                return
            except InvalidCredentialsError:
                _popup("Error", "Incorrect password. OTP blocked.")
                return
            except LegacyOtpUnavailable:
                _popup(
                    "Info",
                    "OTP login is currently available only with your registered phone number. "
                    "Please enter that number to receive an OTP.",
                )
                return
            except Exception as exc:
                _popup("Error", f"Send OTP error:\n{exc}")
                return

        Thread(target=work, daemon=True).start()

    def verify_and_login(self) -> None:
        identifier = self._read_identifier()
        password = self._read_password()
        otp = _safe_text(self, "otp_input")
        if not self._validate_identifier(identifier):
            _popup("Error", "Enter a valid registered email or username.")
            return
        if len(password) < 4:
            _popup("Error", "Enter your account password.")
            return
        if not otp:
            _popup("Error", "Enter the OTP from your email.")
            return

        def work():
            try:
                data: Dict[str, Any] = verify_login_with_otp(identifier, password, otp)
                token: Optional[str] = data.get("access_token") or data.get("token")
                user: Optional[Dict[str, Any]] = data.get("user")

                # If user not returned, try fetching profile
                if token and not user:
                    try:
                        prof = get_profile(token)
                        if isinstance(prof, dict):
                            user = prof.get("user") or prof
                    except Exception:
                        pass

                if not token:
                    _popup("Error", f"Invalid OTP.\n{data}")
                    return

                # Persist locally (FIXED: using set_token / set_user)
                if token:
                    storage.set_token(token)
                if isinstance(user, dict):
                    storage.set_user(user)

                def after_login(*_):
                    if self.manager:
                        self.manager.current = "stage"
                    _popup("Success", "Login successful.")

                Clock.schedule_once(after_login, 0)
            except Exception as e:
                _popup("Error", f"Verify OTP error:\n{e}")

        Thread(target=work, daemon=True).start()
