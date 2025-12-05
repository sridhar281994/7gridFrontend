from __future__ import annotations

from threading import Thread
from typing import Any, Optional

from kivy.clock import Clock
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen

from utils.otp_utils import reset_password


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


class ResetPasswordScreen(Screen):
    """Collects and saves the new password once OTP is verified."""

    current_phone = StringProperty("")
    is_processing = BooleanProperty(False)

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._token: Optional[str] = None
        self._otp: Optional[str] = None

    # ---------- navigation ----------
    def go_back(self) -> None:
        if self.manager:
            self.manager.current = "forgot_password"

    def set_context(self, *, phone: str, token: str, otp: Optional[str] = None) -> None:
        self.current_phone = phone
        self._token = token
        self._otp = otp

    def clear_fields(self) -> None:
        pwd = self.ids.get("password_input")
        confirm = self.ids.get("confirm_password_input")
        if pwd:
            pwd.text = ""
        if confirm:
            confirm.text = ""

    # ---------- actions ----------
    def save_new_password(self) -> None:
        if self.is_processing:
            return
        if not self._token:
            _popup("Error", "OTP session expired. Restart the forgot password flow.")
            return

        password = _safe_text(self, "password_input")
        confirm = _safe_text(self, "confirm_password_input")

        if len(password) < 6:
            _popup("Invalid", "Password must be at least 6 characters.")
            return
        if password != confirm:
            _popup("Invalid", "Passwords do not match.")
            return

        phone = self.current_phone
        otp = self._otp

        def work():
            self.is_processing = True
            try:
                data = reset_password(
                    password,
                    token=self._token,
                    phone=phone,
                    otp=otp,
                )
                ok = bool(data.get("ok", True))
                msg = data.get("message") or ("Password updated." if ok else "Password reset failed.")
                if not ok and not data.get("access_token"):
                    raise RuntimeError(msg)

                def after_save(*_):
                    self.clear_fields()
                    self._token = None
                    self._otp = None
                    _popup("Success", msg or "Password updated.")

                    if self.manager:
                        try:
                            login_screen = self.manager.get_screen("login")
                        except Exception:
                            login_screen = None

                        if login_screen:
                            phone_field = login_screen.ids.get("phone_input")
                            if phone_field and phone:
                                phone_field.text = phone
                            otp_field = login_screen.ids.get("otp_input")
                            if otp_field:
                                otp_field.text = ""

                        self.manager.current = "login"

                Clock.schedule_once(after_save, 0)
            except Exception as e:
                _popup("Error", f"Reset password error:\n{e}")
            finally:
                self.is_processing = False

        Thread(target=work, daemon=True).start()
