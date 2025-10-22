from kivy.app import App
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.clock import Clock
from kivy.graphics import RoundedRectangle, Color
from kivy.properties import StringProperty
from kivy.core.window import Window
import threading, requests

try:
    from utils import storage
except Exception:
    storage = None


class StageScreen(Screen):
    profile_image = StringProperty("assets/default.png")

    def _scale(self, base: float) -> float:
        w, h = Window.size
        scale_factor = min(w / 1080, h / 2400)
        return dp(base * scale_factor * 2.2)

    def _font(self, base_sp: float) -> str:
        w, h = Window.size
        scale_factor = min(w / 1080, h / 2400)
        return f"{max(12, base_sp * scale_factor * 2):.1f}sp"

    def _current_player_name(self) -> str:
        if storage:
            user = storage.get_user() or {}
            if user.get("name"):
                return user["name"].strip()
            if user.get("email"):
                return user["email"].split("@", 1)[0]
            if user.get("profile_image"):
                self.profile_image = user["profile_image"]
        return "You"

    def on_pre_enter(self, *_):
        self._fetch_wallet_from_backend()
        me = self._current_player_name()
        name_lbl = self.ids.get("welcome_label")
        if name_lbl:
            name_lbl.text = me or "Player"
        pic = self.ids.get("profile_pic")
        if pic:
            pic.source = self.profile_image
        self._load_stakes_from_backend()
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if token and backend:
            def worker():
                try:
                    resp = requests.post(
                        f"{backend}/matches/abandon",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                        verify=False,
                    )
                    print(f"[INFO] Auto-abandon response: {resp.status_code} {resp.text}")
                except Exception as e:
                    print(f"[WARN] Auto-abandon failed: {e}")
            threading.Thread(target=worker, daemon=True).start()

    def _load_stakes_from_backend(self):
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        stages_box = self.ids.get("stages_box")
        if not backend or not stages_box:
            return

        def clear_box():
            keep_title = []
            for child in list(stages_box.children):
                if isinstance(child, Label) and child.text == "Select Game Stage":
                    keep_title.append(child)
            stages_box.clear_widgets()
            for child in reversed(keep_title):
                stages_box.add_widget(child)

        def worker():
            try:
                resp = requests.get(
                    f"{backend}/game/stakes",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    timeout=10,
                    verify=False,
                )
                if resp.status_code == 200:
                    stakes = resp.json()
                    Clock.schedule_once(lambda dt: self._populate_stages(stages_box, stakes), 0)
            except Exception as e:
                print(f"[ERR] Stakes fetch failed: {e}")

        clear_box()
        threading.Thread(target=worker, daemon=True).start()

    def _populate_stages(self, stages_box, stakes):
        from kivy.uix.button import Button
        from kivy.graphics import Color, RoundedRectangle

        self._stage_buttons = []
        btn_width = self._scale(220)
        btn_height = self._scale(50)
        fnt = self._font(18)

        for stake in stakes:
            label = stake.get("label", f"₹{stake.get('stake_amount', 0)}")
            amount = stake.get("stake_amount", 0)
            btn = Button(
                text=label,
                size_hint=(None, None),
                size=(btn_width, btn_height),
                font_size=fnt,
                color=(1, 1, 1, 1),
                background_normal="",
                background_color=(0, 0, 0, 0),
                pos_hint={"center_x": 0.5},
            )
            with btn.canvas.before:
                Color(1, 0.5, 0, 1)
                btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size, radius=[12])
            btn.bind(pos=lambda inst, val: setattr(inst._bg, "pos", inst.pos))
            btn.bind(size=lambda inst, val: setattr(inst._bg, "size", inst.size))
            btn.bind(on_release=lambda inst, amt=amount: (self.select_stage(amt), self._highlight_selected(inst)))
            stages_box.add_widget(btn)
            self._stage_buttons.append(btn)

        btn_settings = Button(
            text="Settings",
            size_hint=(None, None),
            size=(btn_width, btn_height),
            font_size=fnt,
            background_normal="",
            background_color=(0.2, 0.2, 0.2, 1),
            color=(1, 1, 1, 1),
            pos_hint={"center_x": 0.5},
        )
        btn_settings.bind(on_release=lambda _: self.go_to_settings())
        stages_box.add_widget(btn_settings)

    def _highlight_selected(self, selected_btn):
        from kivy.graphics import Color, RoundedRectangle
        for btn in self._stage_buttons:
            btn.canvas.before.clear()
            with btn.canvas.before:
                if btn is selected_btn:
                    Color(0.9, 0.3, 0, 1)
                else:
                    Color(1, 0.5, 0, 1)
                btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size, radius=[12])
            btn.bind(pos=lambda inst, val: setattr(inst._bg, "pos", inst.pos))
            btn.bind(size=lambda inst, val: setattr(inst._bg, "size", inst.size))

    def _fetch_wallet_from_backend(self):
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
                    data = resp.json()
                    storage.set_user(data)
                    balance = data.get("wallet_balance", 0)
                    name = data.get("name") or data.get("email", "Player")
                    pic_url = data.get("profile_image") or "assets/default.png"
                    self.profile_image = pic_url
                    Clock.schedule_once(lambda dt: self._update_wallet_label(balance), 0)
                    Clock.schedule_once(lambda dt: self._update_name_label(name), 0)
                    Clock.schedule_once(lambda dt: self._update_profile_pic(pic_url), 0)
            except Exception as e:
                print(f"[ERR] Wallet fetch failed: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _update_wallet_label(self, balance: float):
        lbl = self.ids.get("wallet_label")
        if lbl:
            lbl.text = f"Wallet: ₹{balance}"

    def _update_name_label(self, name: str):
        lbl = self.ids.get("welcome_label")
        if lbl:
            lbl.text = name

    def _update_profile_pic(self, pic_url: str):
        pic = self.ids.get("profile_pic")
        if pic:
            pic.source = pic_url

    def select_stage(self, amount: int):
        app = App.get_running_app()
        mode = getattr(app, "selected_mode", 2)
        App.get_running_app().selected_stake = int(amount)
        App.get_running_app().selected_mode = mode
        me = self._current_player_name()
        if "usermatch" in self.manager.screen_names:
            match_screen = self.manager.get_screen("usermatch")
            match_screen.selected_amount = int(amount)
            match_screen.selected_mode = mode
            if hasattr(match_screen, "start_matchmaking"):
                match_screen.start_matchmaking(local_player_name=me, amount=int(amount), mode=mode)
            self.manager.current = "usermatch"
        else:
            game_screen = self.manager.get_screen("dicegame")
            if hasattr(game_screen, "set_stage_and_players"):
                if mode == 2:
                    game_screen.set_stage_and_players(amount, me, "Bot")
                else:
                    game_screen.set_stage_and_players(amount, me, "Sharp", "Crazy Boy")
            self.manager.current = "dicegame"

    def go_to_settings(self):
        self.manager.current = "settings"

    def confirm_exit(self):
        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text="Are you sure you want to exit?"))
        button_box = BoxLayout(spacing=10, size_hint_y=None, height=self._scale(40))
        btn_yes = Button(text="Yes", font_size=self._font(15))
        btn_no = Button(text="No", font_size=self._font(15))
        button_box.add_widget(btn_yes)
        button_box.add_widget(btn_no)
        layout.add_widget(button_box)
        popup = Popup(
            title="Exit Confirmation",
            content=layout,
            size_hint=(None, None),
            size=(self._scale(300), self._scale(180)),
            auto_dismiss=False,
        )
        btn_yes.bind(on_release=lambda *_: (popup.dismiss(), App.get_running_app().stop()))
        btn_no.bind(on_release=popup.dismiss)
        popup.open()
