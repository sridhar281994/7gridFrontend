from kivy.app import App
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen
from kivy.clock import Clock
from kivy.graphics import RoundedRectangle, Color
from kivy.properties import StringProperty, BooleanProperty
from kivy.core.window import Window
import threading, requests

try:
    from utils import storage
except Exception:
    storage = None


class StageScreen(Screen):
    profile_image = StringProperty("assets/default.png")
    player_description = StringProperty("Describe yourself")
    is_logging_out = BooleanProperty(False)

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
            if user.get("phone"):
                return user["phone"]
            return "Player"
        return "You"

    def on_pre_enter(self, *_):
        self._fetch_wallet_from_backend()

        me = self._current_player_name()
        name_lbl = self.ids.get("welcome_label")
        if name_lbl:
            name_lbl.text = me or "Player"

        cached_user = storage.get_user() if storage else None
        if cached_user:
            self._apply_description_payload(cached_user)

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
                    Clock.schedule_once(
                        lambda dt: self._populate_stages(stages_box, stakes), 0
                    )
            except Exception as e:
                print(f"[ERR] Stakes fetch failed: {e}")

        clear_box()
        threading.Thread(target=worker, daemon=True).start()

    def _populate_stages(self, stages_box, stakes):
        self._stage_buttons = []
        btn_width = self._scale(220)
        btn_height = self._scale(50)
        fnt = self._font(18)

        app = App.get_running_app()
        selected_mode = getattr(app, "selected_mode", 2)

        if storage and hasattr(storage, "set_stakes_cache"):
            try:
                storage.set_stakes_cache(stakes)
            except Exception:
                pass

        for stake in stakes:
            players = int(stake.get("players", 2))
            if players != int(selected_mode):
                continue

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

            # Pass BOTH amount and label so we know if it's ROBOTS Army
            btn.bind(
                on_release=lambda inst, amt=amount, lbl=label: (
                    self.select_stage(amt, lbl),
                    self._highlight_selected(inst),
                )
            )

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
                    desc = self._extract_description(data)
                    if desc and not data.get("description"):
                        data["description"] = desc
                    storage.set_user(data)
                    balance = data.get("wallet_balance", 0)

                    name = (data.get("name") or "").strip()
                    if not name:
                        name = data.get("phone") or "Player"

                    pic_url = data.get("profile_image") or "assets/default.png"
                    self.profile_image = pic_url
                    Clock.schedule_once(
                        lambda dt: self._update_wallet_label(balance), 0
                    )
                    Clock.schedule_once(lambda dt: self._update_name_label(name), 0)
                    Clock.schedule_once(
                        lambda dt: self._update_profile_pic(pic_url), 0
                    )
                    Clock.schedule_once(
                        lambda dt, payload=data: self._apply_description_payload(payload),
                        0,
                    )
                    if not desc:
                        self._fetch_description_from_backend()
            except Exception as e:
                print(f"[ERR] Wallet fetch failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _update_wallet_label(self, balance: float):
        lbl = self.ids.get("wallet_label")
        if lbl:
            if storage:
                lbl.text = storage.wallet_label_text(balance)
            else:
                try:
                    lbl.text = f"Wallet: ₹{int(round(float(balance)))}"
                except (TypeError, ValueError):
                    lbl.text = "Wallet: ₹0"

    def _update_name_label(self, name: str):
        lbl = self.ids.get("welcome_label")
        if lbl:
            lbl.text = name

    def _update_profile_pic(self, pic_url: str):
        pic = self.ids.get("profile_pic")
        if pic:
            pic.source = pic_url

    def _apply_description_payload(self, payload) -> bool:
        desc = self._extract_description(payload)
        if desc:
            self._set_player_description(desc)
            return True
        return False

    @staticmethod
    def _extract_description(payload) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("description", "player_description", "profile_description", "bio", "about"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _set_player_description(self, description: str | None):
        clean = (description or "").strip() if isinstance(description, str) else ""
        fallback = "Describe yourself"
        self.player_description = clean or fallback

    def _fetch_description_from_backend(self):
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (backend and token):
            return

        def worker():
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            desc_text = ""
            for path in ("/users/me/profile", "/users/me"):
                try:
                    resp = requests.get(
                        f"{backend}{path}",
                        headers=headers,
                        timeout=10,
                        verify=False,
                    )
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                    desc_text = self._extract_description(payload)
                    if desc_text:
                        if storage:
                            cached = storage.get_user() or {}
                            cached["description"] = desc_text
                            storage.set_user(cached)
                        Clock.schedule_once(
                            lambda dt, txt=desc_text: self._set_player_description(txt),
                            0,
                        )
                        return
                except Exception as err:
                    print(f"[WARN] Description fetch failed ({path}): {err}")

        threading.Thread(target=worker, daemon=True).start()

    def select_stage(self, amount: int, label: str):
        app = App.get_running_app()
        mode = getattr(app, "selected_mode", 2)
        app.selected_stake = int(amount)
        app.selected_mode = mode

        me = self._current_player_name()

        # ROBOTS Army → offline bots
        if label.strip().lower() == "robots army":
            print("[INFO] ROBOTS Army → offline bot mode")
            if "usermatch" in self.manager.screen_names:
                match_screen = self.manager.get_screen("usermatch")
                match_screen.selected_amount = int(amount)
                match_screen.selected_mode = mode
                match_screen._fallback_to_bots(me)
                self.manager.current = "dicegame"
            else:
                # Hard fallback if for some reason usermatch screen is missing
                game_screen = self.manager.get_screen("dicegame")
                if hasattr(game_screen, "set_stage_and_players"):
                    if mode == 2:
                        game_screen.set_stage_and_players(amount, me, "Bot")
                    else:
                        game_screen.set_stage_and_players(
                            amount, me, "Sharp", "Crazy Boy"
                        )
                self.manager.current = "dicegame"
            return

        # All other stages (including Free Play) → ONLINE
        if "usermatch" in self.manager.screen_names:
            match_screen = self.manager.get_screen("usermatch")
            match_screen.selected_amount = int(amount)
            match_screen.selected_mode = mode
            if hasattr(match_screen, "start_matchmaking"):
                match_screen.start_matchmaking(
                    local_player_name=me, amount=int(amount), mode=mode
                )
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

    def logout_to_login(self):
        """Clear local session data and send the user back to login."""
        if self.is_logging_out:
            return
        self.is_logging_out = True

        def perform_logout(_dt):
            if storage:
                storage.clear_all()

            app = App.get_running_app()
            for attr in ("user_token", "user_id", "selected_stake"):
                if hasattr(app, attr):
                    setattr(app, attr, None)

            if self.manager:
                self.manager.current = "login"

            Clock.schedule_once(self._reset_logout_state, 0.5)

        Clock.schedule_once(perform_logout, 0)

    def _reset_logout_state(self, *_):
        self.is_logging_out = False
