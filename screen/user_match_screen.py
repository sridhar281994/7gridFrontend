import threading
import requests
import random

from kivy.uix.screenmanager import Screen
from kivy.properties import StringProperty, NumericProperty, BooleanProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.animation import Animation

try:
    from utils import storage
except Exception:
    storage = None


class UserMatchScreen(Screen):
    # ---------- Names bound to KV ----------
    player1_name = StringProperty("Waiting...")
    player2_name = StringProperty("Searching...")
    player3_name = StringProperty("") # empty for 2-player

    # ---------- Rotation angles ----------
    p2_angle = NumericProperty(0)
    p3_angle = NumericProperty(0)

    # ---------- State ----------
    selected_amount = NumericProperty(0)
    selected_mode = NumericProperty(2) # 2 or 3 players

    _stop_polling = False
    _poll_event = None
    _rotate_event = None
    _p2_rotating = BooleanProperty(False)
    _p3_rotating = BooleanProperty(False)
    _pulse_anims = []
    _bot_cache = None
    _last_poll_data = {}
    _popup_timer = None # keep a handle to the 12s popup timer

    # -------------------------
    # Lifecycle
    # -------------------------
    def on_pre_enter(self, *_):
        if not self._rotate_event:
            self._rotate_event = Clock.schedule_interval(self._rotate_tick, 1 / 60.0)
        Clock.schedule_once(lambda dt: self._apply_pulse_anims(), 0)

    def on_leave(self, *_):
        if self._rotate_event:
            self._rotate_event.cancel()
            self._rotate_event = None
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None
        self._stop_polling = True
        if self._popup_timer is not None:
            try:
                Clock.unschedule(self._popup_timer)
            except Exception:
                pass
            self._popup_timer = None
        self._stop_pulse_anims()
        self._p2_rotating = False
        self._p3_rotating = False
        self.p2_angle = 0
        self.p3_angle = 0
        self._bot_cache = None

    # -------------------------
    # Rotation driver
    # -------------------------
    def _rotate_tick(self, dt):
        delta_deg = 360 * dt * 2
        if self._p2_rotating:
            self.p2_angle = (self.p2_angle + delta_deg) % 360
        if self.selected_mode == 3 and self._p3_rotating:
            self.p3_angle = (self.p3_angle + delta_deg) % 360

    # -------------------------
    # Start matchmaking
    # -------------------------
    def start_matchmaking(self, local_player_name: str, amount: int, mode: int):
        self.selected_amount = int(amount)
        self.selected_mode = int(mode)
        self._stop_polling = False
        self._bot_cache = None
        self._last_poll_data = {}

        if self._popup_timer is not None:
            try:
                Clock.unschedule(self._popup_timer)
            except Exception:
                pass
            self._popup_timer = None

        self.player1_name = local_player_name or "You"
        self.player2_name = "Searching..."
        self.player3_name = "" if mode == 2 else "Searching..."

        if "p2_pic" in self.ids:
            self.ids.p2_pic.source = "assets/default.png"
        if "p3_pic" in self.ids:
            self.ids.p3_pic.source = "assets/default.png"

        if "p2_name" in self.ids:
            self.ids.p2_name.text = "Searching..."
        if mode == 3 and "p3_name" in self.ids:
            self.ids.p3_name.text = "Searching..."

        self._p2_rotating = True
        self._p3_rotating = (mode == 3)
        self.p2_angle = 0
        self.p3_angle = 0

        Clock.schedule_once(lambda dt: self._apply_pulse_anims(), 0)

        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        if not (token and backend):
            print("[ERR] No backend/token")
            return

        def worker():
            try:
                payload = {"stake_amount": self.selected_amount, "num_players": self.selected_mode}
                resp = requests.post(
                    f"{backend}/matches/create",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload, timeout=10, verify=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    match_id = data.get("match_id")
                    if storage:
                        storage.set_current_match(match_id)
                        storage.set_stake_amount(self.selected_amount)
                        storage.set_num_players(self.selected_mode)
                        storage.set_player_names(local_player_name, None, None)
                        storage.set_player_ids([data.get("p1_id"), data.get("p2_id"), data.get("p3_id")])
                    self._popup_timer = lambda dt: self._show_freeplay_popup(local_player_name)
                    Clock.schedule_once(self._popup_timer, 12)
                else:
                    print(f"[ERR] Match create failed: {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"[ERR] Match create exception: {e}")

        threading.Thread(target=worker, daemon=True).start()

        if self._poll_event:
            self._poll_event.cancel()
        self._poll_event = Clock.schedule_interval(lambda dt: self._poll_match_ready(), 2)

    # -------------------------
    # Pulse "Searching..."
    # -------------------------
    def _apply_pulse_anims(self):
        self._stop_pulse_anims()
        for lbl_id in ("p2_name", "p3_name"):
            lbl = self.ids.get(lbl_id)
            if lbl and "Searching" in (lbl.text or ""):
                anim = (Animation(opacity=0.35, duration=0.4) + Animation(opacity=1.0, duration=0.4))
                anim.repeat = True
                anim.start(lbl)
                self._pulse_anims.append(anim)

    def _stop_pulse_anims(self):
        for anim in self._pulse_anims:
            try:
                anim.stop_all()
            except Exception:
                pass
        self._pulse_anims.clear()
        for lbl_id in ("p2_name", "p3_name"):
            lbl = self.ids.get(lbl_id)
            if lbl:
                lbl.opacity = 1

    # -------------------------
    # Popup for bot fallback
    # -------------------------
    def _show_freeplay_popup(self, local_player_name: str):
        if self._stop_polling:
            return
        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text="No player joined. Play with bot?"))
        btn_box = BoxLayout(spacing=10, size_hint_y=None, height=dp(40))
        btn_yes = Button(text="Yes")
        btn_no = Button(text="No")
        btn_box.add_widget(btn_yes)
        btn_box.add_widget(btn_no)
        layout.add_widget(btn_box)

        popup = Popup(title="Play with Bot?", content=layout,
                      size_hint=(None, None), size=(dp(300), dp(180)), auto_dismiss=False)
        btn_yes.bind(on_release=lambda *_: (popup.dismiss(), self._fallback_to_bots(local_player_name)))
        btn_no.bind(on_release=lambda *_: (popup.dismiss(), self._stop_polling_and_abandon()))
        popup.open()

    # -------------------------
    # Fallback to bots
    # -------------------------
    def _fallback_to_bots(self, local_player_name: str):
        BOT_PROFILES = [
            {"id": -1000, "name": "Sharp", "pic": "assets/bot_sharp.png"},
            {"id": -1001, "name": "Crazy Boy", "pic": "assets/bot_crazy.png"},
            {"id": -1002, "name": "Kurfi", "pic": "assets/bot_kurfi.png"},
        ]
        self._stop_polling = True
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None
        if self._popup_timer is not None:
            try:
                Clock.unschedule(self._popup_timer)
            except Exception:
                pass
            self._popup_timer = None

        self._bot_cache = {}
        players = [local_player_name]
        ids = [None, None, None]
        if self.selected_mode == 2:
            bot = random.choice(BOT_PROFILES)
            players.append(bot["name"])
            ids[1] = bot["id"]
            self._bot_cache["p2"] = bot
            self.ids.p2_pic.source = bot["pic"]
            self.ids.p2_name.text = bot["name"]
            self.player3_name = ""
        else:
            bots = random.sample(BOT_PROFILES, 2)
            players.extend([b["name"] for b in bots])
            ids[1] = bots[0]["id"]
            ids[2] = bots[1]["id"]
            self._bot_cache["p2"] = bots[0]
            self._bot_cache["p3"] = bots[1]
            self.ids.p2_pic.source = bots[0]["pic"]
            self.ids.p2_name.text = bots[0]["name"]
            self.ids.p3_pic.source = bots[1]["pic"]
            self.ids.p3_name.text = bots[1]["name"]

        if storage:
            try:
                storage.set_my_player_index(0)
            except Exception:
                pass

        self._go_game(players, ids)

    # -------------------------
    # Go to game
    # -------------------------
    def _go_game(self, players, ids_or_turn, turn: int = 0):
        self._stop_pulse_anims()
        self._p2_rotating = False
        self._p3_rotating = False
        self._bot_cache = self._bot_cache or {}
        self._stop_polling = True
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None
        if self._popup_timer is not None:
            try:
                Clock.unschedule(self._popup_timer)
            except Exception:
                pass
            self._popup_timer = None

        data = getattr(self, "_last_poll_data", {}) or {}
        if isinstance(ids_or_turn, list):
            pids = ids_or_turn
        else:
            pids = [data.get("p1_id"), data.get("p2_id"), data.get("p3_id")]
            turn = ids_or_turn

        self._remember_my_index(players, pids, data)

        game = self.manager.get_screen("dicegame")
        if self.selected_mode == 2:
            game.set_stage_and_players(self.selected_amount, players[0], players[1],
                                       match_id=data.get("match_id"))
            if "p3_box" in self.ids:
                self.ids.p3_box.opacity = 0
                self.ids.p3_box.disabled = True
        else:
            game.set_stage_and_players(self.selected_amount, players[0], players[1], players[2],
                                       match_id=data.get("match_id"))
            if "p3_box" in self.ids:
                self.ids.p3_box.opacity = 1
                self.ids.p3_box.disabled = False

        if storage:
            storage.set_player_names(*players[: self.selected_mode])
            storage.set_player_ids(pids)
        Clock.schedule_once(lambda dt: game._place_coins_near_portraits(), 0.1)
        self.manager.current = "dicegame"

    def _remember_my_index(self, players, pids, data):
        if not storage:
            return

        my_idx = None
        if data:
            idx = data.get("player_index")
            if idx is not None:
                try:
                    my_idx = int(idx)
                except Exception:
                    my_idx = None

        if my_idx is None:
            uid = storage.get_user_id()
            if uid is not None:
                for i, pid in enumerate(pids or []):
                    try:
                        if pid is not None and int(pid) == int(uid):
                            my_idx = i
                            break
                    except Exception:
                        continue

        if my_idx is None:
            display = ""
            try:
                display = (storage.get_display_name() or "").strip().lower()
            except Exception:
                display = ""
            if display:
                for i, name in enumerate(players or []):
                    if isinstance(name, str) and name.strip().lower() == display:
                        my_idx = i
                        break

        if my_idx is None:
            my_idx = 0

        try:
            storage.set_my_player_index(int(my_idx))
        except Exception:
            pass

    # -------------------------
    # Poll match ready
    # -------------------------
    def _poll_match_ready(self):
        if self._stop_polling:
            return False
        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        match_id = storage.get_current_match() if storage else None
        if not (token and backend and match_id):
            return
        try:
            resp = requests.get(
                f"{backend}/matches/check",
                headers={"Authorization": f"Bearer {token}"},
                params={"match_id": match_id},
                timeout=10, verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._last_poll_data = data
                if data.get("refunded"):
                    print("[INFO] Match refunded -> returning to stage")
                    self._stop_polling = True
                    if self._poll_event:
                        self._poll_event.cancel()
                        self._poll_event = None
                    self.manager.current = "stage"
                    return
                if data.get("ready"):
                    self._stop_polling = True
                    if self._poll_event:
                        self._poll_event.cancel()
                        self._poll_event = None
                    self.selected_mode = int(data.get("num_players") or self.selected_mode)
                    players = [data.get("p1") or "Player 1", data.get("p2") or "Player 2"]
                    if self.selected_mode == 3:
                        players.append(data.get("p3") or "Player 3")
                    pids = [data.get("p1_id"), data.get("p2_id"), data.get("p3_id")]
                    if storage:
                        storage.set_player_ids(pids)
                    self._go_game(players, data.get("turn", 0))
            elif resp.status_code == 404:
                print("[INFO] Match not found -> returning to stage")
                self._stop_polling = True
                if self._poll_event:
                    self._poll_event.cancel()
                    self._poll_event = None
                self.manager.current = "stage"
            else:
                print(f"[WARN] Poll failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[ERR] Poll exception: {e}")

    def _stop_polling_and_abandon(self):
        self._stop_polling = True
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None

        token = storage.get_token() if storage else None
        backend = storage.get_backend_url() if storage else None
        match_id = storage.get_current_match() if storage else None
        if token and backend and match_id:
            def worker():
                try:
                    resp = requests.post(
                        f"{backend}/matches/abandon",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"match_id": match_id}, timeout=10, verify=False,
                    )
                    print(f"[INFO] Abandon response: {resp.status_code} {resp.text}")
                except Exception as e:
                    print(f"[ERR] Abandon failed: {e}")
            threading.Thread(target=worker, daemon=True).start()
        if storage:
            storage.set_current_match(None)
        self.manager.current = "stage"
