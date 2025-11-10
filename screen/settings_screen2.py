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
import threading, requests, os, webbrowser

from utils.settings_utils import save_user_settings
try:
    from utils import storage
except Exception:
    storage = None


class SettingsScreen(Screen):
    music_playing = BooleanProperty(False)
    profile_image = StringProperty("assets/default.png") # bound to KV

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
        token = storage.get_token()
        backend = storage.get_backend_url()

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
                    Clock.schedule_once(update_inputs, 0)
            except Exception as e:
                print(f"[WARN] Failed to preload settings: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Audio ------------------
    def toggle_audio(self):
        if not self.sound:
            return
        if self.music_playing:
            self.sound.stop()
            self.music_playing = False
        else:
            self.sound.play()
            self.music_playing = True

    # ------------------ Profile ------------------
    def change_profile_picture(self):
        """Open file chooser popup to select a new image."""
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
                self.profile_image = file_path # immediate preview

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
        """Update only non-empty fields, keep existing ones if left blank."""
        name = self.ids.name_input.text.strip()
        desc = self.ids.desc_input.text.strip()
        upi = self.ids.upi_input.text.strip()
        phone = self.ids.phone_input.text.strip()

        payload = {}
        if name: # only update if not blank
            payload["name"] = name
        if upi:
            payload["upi_id"] = upi
        if desc:
            payload["description"] = desc

        if not payload:
            self.show_popup("Nothing to update", "Please edit a field first.")
            return

        token = storage.get_token()
        backend = storage.get_backend_url()

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
                    Clock.schedule_once(lambda dt: self.show_popup("Success", "Settings updated!"), 0)
                    Clock.schedule_once(lambda dt: self.refresh_wallet_balance(), 0)
                else:
                    Clock.schedule_once(lambda dt: self.show_popup("Error", f"Update failed: {resp.text}"), 0)
            except Exception as e:
                Clock.schedule_once(lambda dt: self.show_popup("Error", f"Request failed: {e}"), 0)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Recharge ------------------
    def recharge(self):
        """Recharge wallet securely using Razorpay Payment Link."""
        box = BoxLayout(orientation="vertical", spacing=5, padding=5)
        ti = TextInput(hint_text="Enter recharge amount", multiline=False, input_filter="int")
        box.add_widget(ti)
        btn = Button(text="Submit", size_hint_y=None, height=40)
        box.add_widget(btn)
        popup = Popup(title="Recharge", content=box, size_hint=(0.8, 0.4))

        def do_recharge(amount: int):
            token = storage.get_token()
            backend = storage.get_backend_url()

            def worker():
                try:
                    resp = requests.post(
                        f"{backend}/wallet/recharge/create-link",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"amount": amount},
                        timeout=15,
                        verify=False
                    )
                    data = resp.json()
                    url = data.get("short_url")
                    if not url:
                        raise Exception("No payment link returned")

                    Clock.schedule_once(lambda dt: webbrowser.open(url), 0)
                    Clock.schedule_once(lambda dt: self.show_popup("Info", "Complete payment in UPI app. Refresh wallet after payment."), 0)
                except Exception as e:
                    err = str(e)
                    Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", msg), 0)

            threading.Thread(target=worker, daemon=True).start()

        def submit(_):
            try:
                amount = int(ti.text.strip())
                if amount <= 0:
                    raise ValueError
                do_recharge(amount)
                popup.dismiss()
            except Exception:
                self.show_popup("Error", "Invalid amount")

        btn.bind(on_release=submit)
        popup.open()

    # ------------------ Withdraw ------------------
    def withdraw(self):
        """Withdraw securely with balance validation."""
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        user = storage.get_user() or {}
        balance = user.get("wallet_balance") or 0

        if not (token and backend):
            self.show_popup("Error", "Not logged in")
            return

        box = BoxLayout(orientation="vertical", spacing=5, padding=5)
        ti = TextInput(hint_text="Enter withdraw amount", multiline=False, input_filter="int")
        upi = TextInput(hint_text="Enter your UPI ID", multiline=False)
        box.add_widget(ti)
        box.add_widget(upi)
        btn = Button(text="Withdraw", size_hint_y=None, height=40)
        box.add_widget(btn)
        popup = Popup(title="Withdraw", content=box, size_hint=(0.8, 0.5))

        def submit(_):
            try:
                amount = int(ti.text.strip())
                upi_id = upi.text.strip()
                if amount <= 0 or not upi_id:
                    raise ValueError("Invalid input")
                if amount > balance:
                    self.show_popup("Error", "Insufficient wallet balance")
                    return

                def worker():
                    try:
                        resp = requests.post(
                            f"{backend}/wallet/withdraw/request",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"amount": amount, "upi_id": upi_id},
                            timeout=10,
                            verify=False,
                        )
                        if resp.status_code == 200:
                            Clock.schedule_once(lambda dt: self.show_popup("Success", f"Withdrawn ₹{amount} (Pending approval)"), 0)
                            Clock.schedule_once(lambda dt: self.refresh_wallet_balance(), 0)
                        else:
                            err = resp.text
                            Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", f"Withdraw failed: {msg}"), 0)
                    except Exception as e:
                        err = str(e)
                        Clock.schedule_once(lambda dt, msg=err: self.show_popup("Error", f"Withdraw failed: {msg}"), 0)

                threading.Thread(target=worker, daemon=True).start()
                popup.dismiss()
            except Exception as e:
                self.show_popup("Error", f"Invalid input: {e}")

        btn.bind(on_release=submit)
        popup.open()

    # ------------------ Wallet History ------------------
    def show_wallet_history(self):
        """Fetch and display last 20 wallet transactions from backend."""
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
                    lbl = Label(
                        text=f"[{tx['timestamp'][:16]}] {tx['type']} ₹{tx['amount']} ({tx['status']})",
                        halign="left",
                        valign="middle",
                        size_hint_y=None,
                        height=30,
                    )
                    lbl.bind(size=lambda inst, val: setattr(inst, "text_size", inst.size))
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
        """Fetch fresh wallet balance from backend and update storage + UI."""
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None

        def worker():
            balance_text = "Wallet: ₹0"
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
                        storage.set_user(user)
                        balance = user.get("wallet_balance") or 0
                        balance_text = f"Wallet: ₹{balance}"
                except Exception as e:
                    print(f"[WARN] Wallet refresh failed: {e}")

            def update_label(dt):
                wallet_lbl = self.ids.get("wallet_label")
                if wallet_lbl:
                    wallet_lbl.text = balance_text

            Clock.schedule_once(update_label, 0)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Popup helper ------------------
    def show_popup(self, title: str, message: str):
        popup = Popup(title=title, content=Label(text=message),
                      size_hint=(0.7, 0.3))
        popup.open()
