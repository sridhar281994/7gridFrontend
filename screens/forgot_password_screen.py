from __future__ import annotations

from threading import Thread
from typing import Dict, Any, Optional

from kivy.clock import Clock
from kivy.properties import BooleanProperty
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen

from utils.otp_utils import send_otp, verify_otp


def _safe_text(screen: Screen, wid: str) -> str:
    widget = screen.ids.get(wid)
    return (widget.text or "").strip() if widget else ""


def _popup(title: str, msg: str) -> None:
    def _open(*_):
        popup = Popup(
            title=title,
            content=Label(text=str(msg)),
            size_hint=(0.75, 0.35),
            auto_dismiss=True,
        )
        popup.open()
        Clock.schedule_once(lambda _dt: popup.dismiss(), 2.5)

    Clock.schedule_once(_open, 0)


class ForgotPasswordScreen(Screen):
    """Two-step helper screen to drive the password reset flow."""

    otp_stage_ready = BooleanProperty(False)
    is_processing = BooleanProperty(False)

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._pending_phone: Optional[str] = None
        self._latest_token: Optional[str] = None

    # ---------- helpers ----------
    def prefill_phone(self, phone: str | None) -> None:
        phone = (phone or "").strip()
        self._pending_phone = phone or self._pending_phone
        field = self.ids.get("phone_input")
        if field and phone:
            field.text = phone

    def go_back(self) -> None:
        if self.manager:
            self.manager.current = "login"

    # ---------- OTP actions ----------
    def send_reset_otp(self) -> None:
        if self.is_processing:
            return

        phone = _safe_text(self, "phone_input")
        if not (phone.isdigit() and len(phone) == 10):
            _popup("Error", "Enter a valid 10-digit phone number.")
            return

        self._pending_phone = phone

        def work():
            self.is_processing = True
            try:
                data: Dict[str, Any] = send_otp(phone)
                ok = bool(data.get("ok", True))
                msg = data.get("message") or ("OTP sent successfully." if ok else "Failed to send OTP.")

                def after_send(*_):
                    if ok:
                        self.otp_stage_ready = True
                        otp_field = self.ids.get("otp_input")
                        if otp_field:
                            otp_field.disabled = False
                            otp_field.focus = True
                    _popup("Success" if ok else "Error", msg)

                Clock.schedule_once(after_send, 0)
            except Exception as e:
                _popup("Error", f"Send OTP error:\n{e}")
            finally:
                self.is_processing = False

        Thread(target=work, daemon=True).start()

    def verify_otp_and_continue(self) -> None:
        if self.is_processing:
            return
        if not self.otp_stage_ready:
            _popup("Info", "Please send OTP first.")
            return

        otp = _safe_text(self, "otp_input")
        if not otp:
            _popup("Error", "Enter the OTP sent to your email.")
            return

        phone = self._pending_phone or _safe_text(self, "phone_input")
        if not (phone.isdigit() and len(phone) == 10):
            _popup("Error", "Phone number missing. Re-enter and resend OTP.")
            return

        def work():
            self.is_processing = True
            try:
                data: Dict[str, Any] = verify_otp(phone, otp)
                token: Optional[str] = data.get("access_token") or data.get("token")
                if not token:
                    raise RuntimeError(data.get("message") or "Invalid OTP response.")

                def after_verify(*_):
                    self._latest_token = token
                    reset_screen = None
                    if self.manager:
                        try:
                            reset_screen = self.manager.get_screen("reset_password")
                        except Exception:
                            reset_screen = None

                    if reset_screen:
                        reset_screen.set_context(phone=phone, token=token, otp=otp)
                        reset_screen.clear_fields()

                    if self.manager:
                        self.manager.current = "reset_password"

                    _popup("Success", "OTP verified. Set your new password.")

                Clock.schedule_once(after_verify, 0)
            except Exception as e:
                _popup("Error", f"Verify OTP error:\n{e}")
            finally:
                self.is_processing = False

        Thread(target=work, daemon=True).start()
