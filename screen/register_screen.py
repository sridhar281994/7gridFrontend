from threading import Thread
import re

from kivy.clock import Clock
from kivy.uix.popup import Popup
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen

from requests import HTTPError
from utils.otp_utils import register_user
from utils import storage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterScreen(Screen):
    @staticmethod
    def _popup(title: str, msg: str) -> None:
        """Non-blocking popup helper (safe from worker threads)."""

        def _open(*_):
            Popup(
                title=title,
                content=Label(text=msg),
                size_hint=(0.7, 0.3),
                auto_dismiss=True,
            ).open()

        Clock.schedule_once(_open, 0)

    def go_back(self) -> None:
        """Navigate back to login or stage screen."""
        if storage.get_token():
            self.manager.current = "stage"
        else:
            self.manager.current = "login"

    def _get(self, wid: str) -> str:
        """Helper to get and trim text from widget by ID."""
        w = self.ids.get(wid)
        return (w.text or "").strip() if w else ""

    def _validate(self, phone: str, email: str, password: str) -> str | None:
        """Basic validation for registration fields."""
        if not (phone.isdigit() and len(phone) == 10):
            return "Enter a valid 10-digit phone number."
        if not EMAIL_RE.match(email):
            return "Enter a valid email address."
        if len(password) < 6:
            return "Password must be at least 6 characters."
        return None

    def save_profile(self) -> None:
        """Collects input, validates, and calls register API."""
        name = self._get("name_input")
        phone = self._get("phone_input")
        email = self._get("email_input")
        password = self._get("password_input")
        upi = self._get("upi_input")  # optional

        err = self._validate(phone, email, password)
        if err:
            self._popup("Invalid", err)
            return

        if not name:
            self._popup("Invalid", "Please enter your full name.")
            return

        def work():
            try:
                res = register_user(
                    phone=phone,
                    email=email,
                    password=password,
                    name=name.strip(),  # ✅ full name directly from user
                    upi_id=upi or None,
                )

                if res.get("ok"):
                    def done_ok(*_):
                        self._popup("Success", "Registered successfully. Please login.")
                        self.manager.current = "login"
                    Clock.schedule_once(done_ok, 0)
                else:
                    msg = res.get("detail") or res.get("message") or "Registration failed."
                    self._popup("Error", msg)

            except HTTPError as e:
                msg = "Registration error."
                try:
                    data = e.response.json()
                    msg = data.get("detail") or data.get("message") or msg
                except Exception:
                    msg = str(e)
                self._popup("Error", msg)

            except Exception as e:
                self._popup("Error", f"Registration error:\n{e}")

        Thread(target=work, daemon=True).start()
