from kivy.uix.screenmanager import Screen
from kivy.core.audio import SoundLoader
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.popup import Popup
from kivy.uix.label import Label
from kivy.uix.boxlayout import BoxLayout
from kivy.clock import Clock
from kivy.uix.filechooser import FileChooserIconView
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.metrics import dp
from kivy.core.window import Window
import requests
import os

from screen.settings_wallet import WalletActionsMixin
try:
    from utils import storage
except Exception:
    storage = None


class SettingsScreen(WalletActionsMixin, Screen):
    music_playing = BooleanProperty(False)
    profile_image = StringProperty("assets/default.png")  # bound to KV

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._original_upi = ""
        self._otp_payload = None
        self._cached_phone = ""
        self._phone_refresh_inflight = False

    def on_pre_enter(self):
        if not hasattr(self, "sound"):
            self.sound = SoundLoader.load("assets/background.mp3")
            if self.sound:
                self.sound.loop = True

        # refresh wallet balance
        self.refresh_wallet_balance()

        # preload profile picture from cached storage
        cached_user = storage.get_user() if storage else None
        if cached_user and cached_user.get("profile_image"):
            self.profile_image = cached_user["profile_image"]

        if cached_user:
            self._apply_user_inputs(cached_user)

        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (token and backend):
            return

        def worker():
            try:
                resp = requests.get(
                    f"{backend}/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code == 200:
                    user = resp.json()
                    if storage:
                        storage.set_user(user)
                    Clock.schedule_once(lambda dt: self._apply_user_inputs(user), 0)
            except Exception as e:
                print(f"[WARN] Failed to preload settings: {e}")

        self._run_async(worker)

    # ------------------ Audio ------------------
    def toggle_audio(self):
        if not hasattr(self, "sound") or not self.sound:
            return
        if self.music_playing:
            self.sound.stop()
            self.music_playing = False
        else:
            self.sound.play()
            self.music_playing = True

    # ------------------ Profile ------------------
    def change_profile_picture(self):
        layout = BoxLayout(orientation="vertical", spacing=5, padding=5)
        filechooser = FileChooserIconView(path=".", filters=["*.png", "*.jpg", "*.jpeg"])
        layout.add_widget(filechooser)

        btn_box = BoxLayout(size_hint_y=None, height=40, spacing=5)
        btn_select = Button(text="Select")
        btn_cancel = Button(text="Cancel")
        btn_box.add_widget(btn_select)
        btn_box.add_widget(btn_cancel)
        layout.add_widget(btn_box)

        popup = Popup(title="Select Profile Picture", content=layout, size_hint=(0.9, 0.9))

        def select_and_upload(*_):
            if filechooser.selection:
                file_path = filechooser.selection[0]
                self.profile_image = file_path

                def worker():
                    try:
                        token = storage.get_token() if storage else None
                        backend = storage.get_backend_url() if storage else None
                        if not (token and backend):
                            raise Exception("Missing token or backend")

                        with open(file_path, "rb") as f:
                            resp = requests.post(
                                f"{backend}/users/upload-profile-image",
                                headers={"Authorization": f"Bearer {token}"},
                                files={"file": (os.path.basename(file_path), f, "image/jpeg")},
                                timeout=15,
                                verify=False,
                            )

                        if resp.status_code == 200:
                            data = resp.json()
                            new_url = data.get("url") or file_path
                            self.profile_image = new_url
                            if storage:
                                user = storage.get_user() or {}
                                user["profile_image"] = new_url
                                storage.set_user(user)
                            Clock.schedule_once(lambda dt: self.show_popup("Success", "Pic saved"), 0)
                        else:
                            err = resp.text
                            Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", "Upload fail", msg), 0)
                    except Exception as e:
                        err = str(e)
                        Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", "Upload fail", msg), 0)

                self._run_async(worker)
            popup.dismiss()

        btn_select.bind(on_release=select_and_upload)
        btn_cancel.bind(on_release=popup.dismiss)
        popup.open()

    # ------------------ Settings ------------------
    def save_settings(self):
        name = self.ids.name_input.text.strip()
        desc = self.ids.desc_input.text.strip()
        upi = self.ids.upi_input.text.strip()
        phone = self.ids.phone_input.text.strip()

        payload = {}
        if name:
            payload["name"] = name
        if desc:
            payload["description"] = desc
        if upi:
            payload["upi_id"] = upi

        if not payload:
            self.show_popup("Update", "Edit first")
            return

        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (token and backend):
            self.show_popup("Error", "Login need")
            return

        # determine whether UPI change needs OTP
        if self._upi_changed(payload):
            self._handle_upi_payment_change(payload, token, backend)
            return

        self._submit_settings(payload, token, backend)

    def _upi_changed(self, payload):
        if "upi_id" not in payload:
            return False
        user = storage.get_user() if storage else None
        original = (user or {}).get("upi_id") or self._original_upi or ""
        return payload["upi_id"].strip() != original.strip()

    def _handle_upi_payment_change(self, payload, token, backend):
        raw_phone = self._get_user_phone()
        otp_phone = self._sanitize_phone_for_otp(raw_phone)
        if otp_phone:
            self._prompt_payment_otp(payload, otp_phone, token, backend)
            return
        if raw_phone:
            self.show_popup("Error", "Phone digits")
            return
        self._refresh_phone_and_retry(payload, token, backend)

    def _refresh_phone_and_retry(self, payload, token, backend):
        if self._phone_refresh_inflight:
            self.show_popup("Info", "Wait phone")
            return
        if not (token and backend):
            self.show_popup("Error", "Login again")
            return

        self._phone_refresh_inflight = True

        def worker():
            fetched_phone = ""
            error_msg = ""
            try:
                resp = requests.get(
                    f"{backend}/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code == 200:
                    user = resp.json()
                    fetched_phone = self._extract_phone(user)
                    if storage:
                        storage.set_user(user)
                    Clock.schedule_once(lambda dt: self._apply_user_inputs(user), 0)
                else:
                    error_msg = resp.text or f"HTTP {resp.status_code}"
            except Exception as exc:
                error_msg = str(exc)

            def finish(_dt):
                self._phone_refresh_inflight = False
                otp_phone = self._sanitize_phone_for_otp(fetched_phone)
                if otp_phone:
                    self._prompt_payment_otp(payload, otp_phone, token, backend)
                else:
                    if error_msg:
                        self.show_popup("Error", "Phone fetch fail", error_msg)
                    else:
                        self.show_popup("Error", "Add phone")

            Clock.schedule_once(finish, 0)

        self._run_async(worker)

    def _prompt_payment_otp(self, payload, phone, token, backend):
        otp_input = TextInput(
            hint_text="Enter OTP",
            password=True,
            size_hint_y=None,
            height=40,
            input_filter="int",
        )
        status_label = Label(
            text=f"Sending OTP to {phone}...",
            size_hint_y=None,
            height=40,
            halign="center",
            valign="middle",
        )
        status_label.bind(size=lambda inst, _: setattr(inst, "text_size", inst.size))
        info_label = Label(
            text="We sent a verification code to your registered phone/email. Enter it below to confirm the changes.",
            size_hint_y=None,
            height=80,
            halign="center",
            valign="middle",
        )
        info_label.bind(size=lambda inst, _: setattr(inst, "text_size", inst.size))
        save_btn = Button(text="Verify & Save", size_hint_y=None, height=45)

        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(info_label)
        layout.add_widget(otp_input)
        layout.add_widget(status_label)
        layout.add_widget(save_btn)

        popup = Popup(
            title="OTP Verification",
            content=layout,
            size_hint=(0.9, None),
            height=320,
            auto_dismiss=False,
        )

        def verify_and_save(*_):
            code = otp_input.text.strip()
            if len(code) < 4:
                status_label.text = "Enter the OTP sent to your email."
                return

            def task():
                try:
                    self._verify_payment_otp(phone, code, backend)
                except Exception as exc:
                    Clock.schedule_once(lambda dt, msg=str(exc): self.show_popup("Error", "OTP fail", msg), 0)
                    return
                Clock.schedule_once(lambda dt: self._submit_settings(payload, token, backend), 0)

            self._run_async(task)
            popup.dismiss()

        save_btn.bind(on_release=verify_and_save)
        popup.open()
        self._send_payment_otp(phone, backend, status_label, token)

    def _send_payment_otp(self, phone, backend, status_label=None, token=None):
        def worker():
            try:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = requests.post(
                    f"{backend}/auth/send-otp",
                    json={"phone": phone},
                    headers=headers,
                    timeout=10,
                    verify=False,
                )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(resp.text or "Failed to send OTP")
                message = "OTP sent successfully."
            except Exception as exc:
                message = f"OTP request failed: {exc}"

            def update_label(dt):
                if status_label:
                    status_label.text = message
                else:
                    self.show_popup("OTP", "OTP info", message)

            Clock.schedule_once(update_label, 0)

        self._run_async(worker)

    def _verify_payment_otp(self, phone, otp_code, backend):
        resp = requests.post(
            f"{backend}/auth/verify-otp",
            json={"phone": phone, "otp": otp_code},
            timeout=10,
            verify=False,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(resp.text or "OTP verification failed")
        return resp.json()

    def _submit_settings(self, payload, token, backend):
        def worker():
            try:
                resp = requests.patch(
                    f"{backend}/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                    timeout=10,
                    verify=False,
                )
                if resp.status_code == 200:
                    user = resp.json()
                    if storage:
                        storage.set_user(user)
                    Clock.schedule_once(lambda dt: self._apply_user_inputs(user), 0)
                    Clock.schedule_once(lambda dt: self.show_popup("Success", "Save done"), 0)
                    Clock.schedule_once(lambda dt: self.refresh_wallet_balance(), 0)
                else:
                    Clock.schedule_once(
                        lambda dt, msg=resp.text or "Unknown error": self.show_popup("Error", "Save fail", msg),
                        0,
                    )
            except Exception as e:
                Clock.schedule_once(lambda dt, msg=str(e): self.show_popup("Error", "Save fail", msg), 0)

        self._run_async(worker)

    def _apply_user_inputs(self, user):
        if not user:
            return
        self._original_upi = user.get("upi_id") or ""
        phone_value = self._extract_phone(user)
        if phone_value:
            self._cached_phone = phone_value

        def updater(_dt):
            if self.ids.get("name_input"):
                self.ids.name_input.text = user.get("name") or ""
            if self.ids.get("upi_input"):
                self.ids.upi_input.text = user.get("upi_id") or ""
            if self.ids.get("desc_input"):
                self.ids.desc_input.text = user.get("description") or ""
            if self.ids.get("phone_input"):
                self.ids.phone_input.text = self._cached_phone

        Clock.schedule_once(updater, 0)

    def _get_user_phone(self):
        if self._cached_phone:
            return self._cached_phone
        if storage:
            user = storage.get_user() or {}
            phone = self._extract_phone(user)
            if phone:
                self._cached_phone = phone
                return phone
        phone_input = self.ids.get("phone_input")
        if phone_input:
            phone = phone_input.text.strip()
            if phone:
                self._cached_phone = phone
            return phone
        return ""

    @staticmethod
    def _sanitize_phone_for_otp(phone: str) -> str:
        if not phone:
            return ""
        digits_only = "".join(ch for ch in phone if ch.isdigit())
        return digits_only if len(digits_only) >= 10 else ""

    def _extract_phone(self, user: dict) -> str:
        if not isinstance(user, dict):
            return ""
        for key in (
            "phone",
            "phone_number",
            "phoneNumber",
            "phone_no",
            "mobile",
            "mobile_number",
            "mobileNumber",
            "mobile_no",
            "contact_phone",
            "contactPhone",
            "contact",
        ):
            value = user.get(key)
            if value:
                return str(value).strip()
        return ""

    def show_popup(self, title: str, message: str, detail: str | None = None, duration: float = 2.5):
        """Responsive popup that auto-closes with concise text."""
        primary = self._simple_words(message)
        lines = [primary]
        if detail:
            lines.append(detail.strip())

        body = "\n".join(line for line in lines if line)
        label = Label(text=body, halign="center", valign="middle")
        label.bind(size=lambda inst, _: setattr(inst, "text_size", inst.size))

        box = BoxLayout(padding=dp(12))
        box.add_widget(label)

        popup = Popup(
            title=title,
            content=box,
            size_hint=(0.8, None),
            height=self._popup_height(),
            auto_dismiss=True,
        )
        popup.open()
        Clock.schedule_once(lambda dt: popup.dismiss(), duration)
        return popup

    def _popup_height(self):
        base = self.height or Window.height or 640
        return max(min(base * 0.3, dp(360)), dp(160))

    @staticmethod
    def _simple_words(text: str, limit: int = 3) -> str:
        if not text:
            return ""
        replacements = {
            "failed": "fail",
            "failure": "fail",
            "missing": "miss",
            "please": "",
            "invalid": "bad",
            "complete": "finish",
            "payment": "pay",
            "number": "num",
            "digits": "digits",
            "before": "before",
            "updating": "update",
            "updated": "update",
        }
        words = []
        for raw in text.split():
            clean = raw.strip(",.!?:;").lower()
            clean = replacements.get(clean, clean)
            if not clean:
                continue
            words.append(clean)
            if len(words) >= limit:
                break
        if not words:
            return text.strip()
        return " ".join(words).title()

