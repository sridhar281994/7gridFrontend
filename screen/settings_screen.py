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
from kivy.uix.scrollview import ScrollView
import threading
import requests
import os
import webbrowser

try:
    from utils import storage
except Exception:
    storage = None


class SettingsScreen(Screen):
    music_playing = BooleanProperty(False)
    profile_image = StringProperty("assets/default.png")

    WALLET_WEB_URL = os.getenv("WALLET_WEB_URL", "").rstrip("/")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._original_upi = ""
        self._original_paypal = ""

    def on_pre_enter(self):
        if not hasattr(self, "sound"):
            self.sound = SoundLoader.load("assets/background.mp3")
            if self.sound:
                self.sound.loop = True

        # refresh wallet balance
        self.refresh_wallet_balance()

        # preload profile picture
        if storage:
            user = storage.get_user() or {}
            if user.get("profile_image"):
                self.profile_image = user["profile_image"]

        # 🔄 preload name, upi, description, phone from backend
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None

        def worker():
            if not (token and backend):
                return
            try:
                resp = requests.get(
                    f"{backend}/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code == 200:
                    user = resp.json()
                    storage.set_user(user)

                    def update_inputs(dt):
                        if self.ids.get("name_input"):
                            self.ids.name_input.text = user.get("name") or ""
                        if self.ids.get("upi_input"):
                            self.ids.upi_input.text = user.get("upi_id") or ""
                        if self.ids.get("desc_input"):
                            self.ids.desc_input.text = user.get("description") or ""
                        if self.ids.get("phone_input"):
                            self.ids.phone_input.text = user.get("phone") or ""

                        # keep original values sync'd for OTP validation
                        self._original_upi = user.get("upi_id") or ""
                        self._original_paypal = user.get("paypal_id") or ""

                    Clock.schedule_once(update_inputs, 0)
            except Exception as e:
                print(f"[WARN] Failed to preload settings: {e}")

        threading.Thread(target=worker, daemon=True).start()

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
                            Clock.schedule_once(lambda dt: self.show_popup("Success", "Profile picture updated!"), 0)
                        else:
                            err = resp.text
                            Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", f"Upload failed: {msg}"), 0)
                    except Exception as e:
                        err = str(e)
                        Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", f"Upload failed: {msg}"), 0)

                threading.Thread(target=worker, daemon=True).start()
            popup.dismiss()

        btn_select.bind(on_release=select_and_upload)
        btn_cancel.bind(on_release=popup.dismiss)
        popup.open()

    # ------------------ Settings ------------------
    def save_settings(self):
        name = self.ids.name_input.text.strip()
        desc = self.ids.desc_input.text.strip()
        paypal_widget = self.ids.get("paypal_input")
        paypal = paypal_widget.text.strip() if paypal_widget else ""
        upi_widget = self.ids.get("upi_input")
        upi = upi_widget.text.strip() if upi_widget else ""

        payload = {}
        if name:
            payload["name"] = name
        if desc:
            payload["description"] = desc

        original_upi = (self._original_upi or "").strip()
        original_paypal = (self._original_paypal or "").strip()
        if upi and upi != original_upi:
            payload["upi_id"] = upi
        if paypal and paypal != original_paypal:
            payload["paypal_id"] = paypal

        if not payload:
            self.show_popup("Nothing to update", "Please edit a field first.")
            return

        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (token and backend):
            self.show_popup("Error", "Missing token or backend.")
            return

        if self._needs_payment_otp(payload):
            self._prompt_payment_otp(payload, token, backend)
            return

        self._submit_settings(payload, token, backend)

    def _populate_user_inputs(self, user):
        if not user:
            return

        def apply_inputs(_dt):
            self._original_upi = user.get("upi_id") or ""
            self._original_paypal = user.get("paypal_id") or ""

            name_input = self.ids.get("name_input")
            if name_input is not None:
                name_input.text = user.get("name") or ""

            desc_input = self.ids.get("desc_input")
            if desc_input is not None:
                desc_input.text = user.get("description") or ""

            phone_input = self.ids.get("phone_input")
            if phone_input is not None:
                phone_value = (
                    user.get("phone")
                    or user.get("phone_number")
                    or user.get("mobile")
                )
                phone_input.text = phone_value or ""

            upi_input = self.ids.get("upi_input")
            if upi_input is not None:
                upi_input.text = user.get("upi_id") or ""

            paypal_input = self.ids.get("paypal_input")
            if paypal_input is not None:
                paypal_input.text = user.get("paypal_id") or ""

        Clock.schedule_once(apply_inputs, 0)

    def _needs_payment_otp(self, payload):
        return any(key in payload for key in ("upi_id", "paypal_id"))

    def _prompt_payment_otp(self, payload, token, backend):
        phone = self._get_user_phone()
        if not phone:
            self.show_popup("Error", "Add a verified phone number before updating payment info.")
            return
        if not phone.isdigit():
            self.show_popup("Error", "Phone number must contain digits only for OTP verification.")
            return

        otp_input = TextInput(
            hint_text="Enter OTP from email",
            password=True,
            size_hint_y=None,
            height=40,
            input_filter="int",
        )
        status_label = Label(
            text="Sending OTP...",
            size_hint_y=None,
            height=50,
            halign="center",
            valign="middle",
        )
        status_label.bind(size=self._auto_resize_label)

        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        info_label = Label(
            text="We are sending a verification code to your registered email. Enter it below and press Save to confirm the changes.",
            size_hint_y=None,
            height=90,
            halign="center",
            valign="middle",
        )
        info_label.bind(size=self._auto_resize_label)
        save_btn = Button(
            text="Save",
            size_hint_y=None,
            height=45,
            background_color=(0.2, 0.7, 0.2, 1),
            color=(1, 1, 1, 1),
        )

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
            otp_code = otp_input.text.strip()
            if len(otp_code) < 4:
                status_label.text = "Enter the OTP sent to your email."
                return

            def worker():
                try:
                    self._verify_payment_otp(backend, phone, otp_code)
                except Exception as exc:
                    Clock.schedule_once(lambda dt, msg=str(exc): self.show_popup("Error", msg), 0)
                    return
                Clock.schedule_once(lambda dt: self._submit_settings(payload, token, backend), 0)

            threading.Thread(target=worker, daemon=True).start()
            popup.dismiss()

        save_btn.bind(on_release=verify_and_save)

        popup.open()
        self._send_payment_otp_email(backend, phone, status_label)

    def _send_payment_otp_email(self, backend, phone, status_label=None):
        if not backend:
            if status_label:
                status_label.text = "Backend URL missing."
            else:
                self.show_popup("Error", "Missing backend.")
            return

        if status_label:
            status_label.text = f"Sending OTP to {phone}..."

        def worker():
            try:
                resp = requests.post(
                    f"{backend}/auth/send-otp",
                    json={"phone": phone},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(resp.text or "Failed to send OTP.")
                message = "OTP sent successfully. Please check your phone/email."
            except Exception as exc:
                message = f"OTP request failed: {exc}"

            def update_status(_dt):
                if status_label:
                    status_label.text = message
                else:
                    self.show_popup("OTP", message)

            Clock.schedule_once(update_status, 0)

        threading.Thread(target=worker, daemon=True).start()

    def _verify_payment_otp(self, backend, phone, otp_code):
        resp = requests.post(
            f"{backend}/auth/verify-otp",
            json={"phone": phone, "otp": otp_code},
            timeout=10,
            verify=False,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(resp.text or "OTP verification failed.")
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
                    storage.set_user(user)
                    self._populate_user_inputs(user)
                    Clock.schedule_once(lambda dt: self.show_popup("Success", "Settings updated!"), 0)
                    Clock.schedule_once(lambda dt: self.refresh_wallet_balance(), 0)
                else:
                    error_text = resp.text or "Unknown error"
                    Clock.schedule_once(lambda dt: self.show_popup("Error", f"Update failed: {error_text}"), 0)
            except Exception as e:
                Clock.schedule_once(lambda dt: self.show_popup("Error", f"Request failed: {e}"), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _get_user_phone(self):
        user_phone = ""
        if storage:
            user = storage.get_user() or {}
            user_phone = (
                user.get("phone")
                or user.get("phone_number")
                or user.get("mobile")
                or ""
            ).strip()

        if not user_phone:
            phone_input = self.ids.get("phone_input")
            if phone_input:
                user_phone = phone_input.text.strip()

        return user_phone

    def _auto_resize_label(self, instance, _value):
        instance.text_size = instance.size

    # ------------------ Wallet portal helpers ------------------
    def _resolve_wallet_base_url(self) -> str:
        if storage:
            custom = getattr(storage, "get_wallet_url", lambda: None)()
            if custom:
                return custom.rstrip("/")
        if self.WALLET_WEB_URL:
            return self.WALLET_WEB_URL
        backend = storage.get_backend_url() if storage else ""
        if backend:
            return backend.rstrip("/").replace("/api", "")
        return ""

    def open_wallet_portal(self, reason: str = "wallet"):
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        wallet_url = self._resolve_wallet_base_url()

        if not (token, backend, wallet_url):
            self.show_popup("Error", "Wallet portal not configured. Update WALLET_WEB_URL.")
            return

        def worker():
            try:
                resp = requests.post(
                    f"{backend}/auth/wallet-link",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code != 200:
                    raise Exception(resp.text or "Failed to create wallet link")

                link_token = resp.json().get("token")
                if not link_token:
                    raise Exception("Wallet link token missing")

                final_url = f"{wallet_url}/link?token={link_token}&source={reason}"
                Clock.schedule_once(lambda dt: webbrowser.open(final_url), 0)
                Clock.schedule_once(
                    lambda dt: self.show_popup(
                        "Wallet Portal",
                        "Wallet site opened in your browser. Complete recharge/withdraw there.",
                    ),
                    0,
                )
            except Exception as exc:
                Clock.schedule_once(
                    lambda dt, msg=str(exc): self.show_popup("Error", f"Unable to open wallet portal: {msg}"),
                    0,
                )

        threading.Thread(target=worker, daemon=True).start()

    def recharge(self):
        self.open_wallet_portal("recharge")

    def withdraw_coins(self):
        self.open_wallet_portal("withdraw")

    # ------------------ Wallet History ------------------
    def show_wallet_history(self):
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (token and backend):
            self.show_popup("Error", "Not logged in or backend missing")
            return

        def worker():
            try:
                resp = requests.get(
                    f"{backend}/wallet/history",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": 20},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code != 200:
                    raise Exception(resp.text)
                data = resp.json()
                txs = data if isinstance(data, list) else data.get("transactions", [])
                if not txs:
                    Clock.schedule_once(lambda dt: self.show_popup("Wallet History", "No transactions found."), 0)
                    return

                layout = BoxLayout(orientation="vertical", spacing=5, padding=10, size_hint_y=None)
                layout.bind(minimum_height=layout.setter("height"))

                for tx in txs:
                    timestamp = tx.get("timestamp") or ""
                    amount = tx.get("amount") or tx.get("coins") or 0
                    lbl = Label(
                        text=f"[{timestamp[:16]}] {tx['type']} {amount} coins ({tx['status']})",
                        halign="left",
                        valign="middle",
                        size_hint_y=None,
                        height=30,
                    )
                    lbl.bind(size=self._auto_resize_label)
                    layout.add_widget(lbl)

                scroll = ScrollView(size_hint=(1, 1))
                scroll.add_widget(layout)

                popup = Popup(
                    title="Wallet History (Last 20)",
                    content=scroll,
                    size_hint=(0.9, 0.7),
                )
                Clock.schedule_once(lambda dt: popup.open(), 0)

            except Exception as e:
                err = str(e)
                Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", f"Failed to fetch history: {msg}"), 0)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Wallet Refresh ------------------
    def refresh_wallet_balance(self):
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None

        def worker():
            balance_text = "Wallet: 0 coins"
            if token and backend:
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
                        balance = (
                            user.get("wallet_balance")
                            if user.get("wallet_balance") is not None
                            else user.get("coin_balance", 0)
                        )
                        balance_text = f"Wallet: {balance} coins"
                except Exception as e:
                    print(f"[WARN] Wallet refresh failed: {e}")

            def update_label(dt):
                wallet_lbl = self.ids.get("wallet_label")
                if wallet_lbl:
                    wallet_lbl.text = balance_text

            Clock.schedule_once(update_label, 0)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Popup helper ------------------
    def show_popup(self, title: str, message: str, auto_dismiss_after: float = 2.5):
        popup = Popup(
            title=title,
            content=Label(text=message),
            size_hint=(0.7, 0.3),
        )
        popup.open()
        if auto_dismiss_after:
            Clock.schedule_once(lambda dt: popup.dismiss(), auto_dismiss_after)
        return popup
