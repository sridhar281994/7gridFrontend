import requests
import threading
import webbrowser

from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

try:
    from utils import storage
except Exception:
    storage = None


class WalletActionsMixin:
    """Reusable wallet-related actions to keep the settings screen lean."""

    @staticmethod
    def _auth_pair():
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        return token, backend

    def _require_auth(self):
        token, backend = self._auth_pair()
        if not (token and backend):
            self.show_popup("Error", "Login need")
            return None, None
        return token, backend

    @staticmethod
    def _run_async(worker):
        threading.Thread(target=worker, daemon=True).start()

    # ------------------ Recharge ------------------
    def recharge(self):
        box = BoxLayout(orientation="vertical", spacing=5, padding=5)
        amount_input = TextInput(hint_text="Enter recharge amount", multiline=False, input_filter="int")
        box.add_widget(amount_input)
        submit_btn = Button(text="Submit", size_hint_y=None, height=40)
        box.add_widget(submit_btn)
        popup = Popup(title="Recharge", content=box, size_hint=(0.8, 0.4))

        def send_request(amount: int):
            token, backend = self._require_auth()
            if not (token and backend):
                return

            def worker():
                try:
                    resp = requests.post(
                        f"{backend}/wallet/recharge/create-link",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"amount": amount},
                        timeout=15,
                        verify=False,
                    )
                    data = resp.json()
                    url = data.get("short_url")
                    if not url:
                        raise RuntimeError("No link")

                    Clock.schedule_once(lambda dt: webbrowser.open(url), 0)
                    Clock.schedule_once(
                        lambda dt: self.show_popup("Info", "Pay app", "Finish payment then refresh"),
                        0,
                    )
                except Exception as err:
                    Clock.schedule_once(lambda dt, msg=str(err): self.show_popup("Error", "Recharge fail", msg), 0)

            self._run_async(worker)

        def submit(_):
            try:
                amount = int(amount_input.text.strip())
                if amount <= 0:
                    raise ValueError
            except Exception:
                self.show_popup("Error", "Amount bad")
                return
            popup.dismiss()
            send_request(amount)

        submit_btn.bind(on_release=submit)
        popup.open()

    # ------------------ Withdraw ------------------
    def withdraw(self):
        token, backend = self._require_auth()
        if not (token and backend):
            return
        user = storage.get_user() or {}
        balance = user.get("wallet_balance") or 0

        box = BoxLayout(orientation="vertical", spacing=5, padding=5)
        amount_input = TextInput(hint_text="Enter withdraw amount", multiline=False, input_filter="int")
        upi_input = TextInput(hint_text="Enter your UPI ID", multiline=False)
        box.add_widget(amount_input)
        box.add_widget(upi_input)
        submit_btn = Button(text="Withdraw", size_hint_y=None, height=40)
        box.add_widget(submit_btn)
        popup = Popup(title="Withdraw", content=box, size_hint=(0.8, 0.5))

        def submit(_):
            try:
                amount = int(amount_input.text.strip())
                upi_id = upi_input.text.strip()
                if amount <= 0 or not upi_id:
                    raise ValueError
                if amount > balance:
                    self.show_popup("Error", "Low wallet")
                    return
            except Exception as exc:
                self.show_popup("Error", "Input bad", str(exc))
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
                        Clock.schedule_once(lambda dt: self.show_popup("Success", "Withdraw sent", f"₹{amount} pending"), 0)
                        Clock.schedule_once(lambda dt: self.refresh_wallet_balance(), 0)
                    else:
                        raise RuntimeError(resp.text or "No response")
                except Exception as err:
                    Clock.schedule_once(lambda dt, msg=str(err): self.show_popup("Error", "Withdraw fail", msg), 0)

            self._run_async(worker)
            popup.dismiss()

        submit_btn.bind(on_release=submit)
        popup.open()

    # ------------------ Wallet History ------------------
    def show_wallet_history(self):
        token, backend = self._require_auth()
        if not (token and backend):
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
                    raise RuntimeError(resp.text)
                data = resp.json()
                txs = data if isinstance(data, list) else data.get("transactions", [])
                if not txs:
                    Clock.schedule_once(lambda dt: self.show_popup("History", "No data"), 0)
                    return

                layout = BoxLayout(orientation="vertical", spacing=5, padding=10, size_hint_y=None)
                layout.bind(minimum_height=layout.setter("height"))

                for tx in txs:
                    text = f"[{tx['timestamp'][:16]}] {tx['type']} ₹{tx['amount']} ({tx['status']})"
                    lbl = Label(
                        text=text,
                        halign="left",
                        valign="middle",
                        size_hint_y=None,
                        height=30,
                    )
                    lbl.bind(size=lambda inst, _: setattr(inst, "text_size", inst.size))
                    layout.add_widget(lbl)

                scroll = ScrollView(size_hint=(1, 1))
                scroll.add_widget(layout)

                popup = Popup(
                    title="Wallet History (Last 20)",
                    content=scroll,
                    size_hint=(0.9, 0.7),
                )
                Clock.schedule_once(lambda dt: popup.open(), 0)

            except Exception as err:
                Clock.schedule_once(lambda dt, msg=str(err): self.show_popup("Error", "History fail", msg), 0)

        self._run_async(worker)

    # ------------------ Wallet Refresh ------------------
    def refresh_wallet_balance(self):
        token, backend = self._auth_pair()

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
                        if storage:
                            storage.set_user(user)
                        balance = user.get("wallet_balance") or 0
                        balance_text = f"Wallet: ₹{balance}"
                except Exception as err:
                    print(f"[WARN] Wallet refresh failed: {err}")

            def update_label(_dt):
                wallet_lbl = self.ids.get("wallet_label")
                if wallet_lbl:
                    wallet_lbl.text = balance_text

            Clock.schedule_once(update_label, 0)

        self._run_async(worker)
