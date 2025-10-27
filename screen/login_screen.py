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
from utils.otp_utils import send_otp, verify_otp, get_profile


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

    # ---------- UI actions (bound in KV) ----------
    def send_otp_to_user(self) -> None:
        phone = _safe_text(self, "phone_input")
        if not (phone.isdigit() and len(phone) == 10):
            _popup("Error", "Enter a valid 10-digit phone number.")
            return

        def work():
            try:
                data: Dict[str, Any] = send_otp(phone)
                ok = bool(data.get("ok", True))  # some backends only return message
                msg = data.get("message") or ("OTP sent successfully." if ok else "Failed to send OTP.")
                _popup("Success" if ok else "Error", msg)
            except Exception as e:
                _popup("Error", f"Send OTP error:\n{e}")

        Thread(target=work, daemon=True).start()

    def verify_and_login(self) -> None:
        phone = _safe_text(self, "phone_input")
        otp = _safe_text(self, "otp_input")
        if not (phone.isdigit() and len(phone) == 10):
            _popup("Error", "Enter a valid 10-digit phone number.")
            return
        if not otp:
            _popup("Error", "Enter the OTP from your email.")
            return

        def work():
            try:
                data: Dict[str, Any] = verify_otp(phone, otp)
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
