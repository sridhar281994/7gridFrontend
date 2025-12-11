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
    # ---------- helpers ----------
    def _resolve_my_index_from_ids(self, ids):
        if not storage or not isinstance(ids, (list, tuple)):
            return None
        user = storage.get_user() or {}
        uid = user.get("id") or user.get("_id")
        if uid is None:
            return None
        for idx, pid in enumerate(ids):
            if pid is not None and str(pid) == str(uid):
                return idx
        return None

    def _resolve_my_index_from_payload(self, payload, trusted: bool = False):
        if not payload:
            return None
        idx = self._resolve_my_index_from_ids(payload.get("player_ids"))
        if idx is not None:
            return idx
        if trusted and payload.get("player_index") is not None:
            try:
                return int(payload.get("player_index"))
            except (TypeError, ValueError):
                return None
        return None

    # ---------- Names bound to KV ----------
    player1_name = StringProperty("Waiting...")
    player2_name = StringProperty("Searching...")
    player3_name = StringProperty("")  # empty for 2-player

    # ---------- Rotation angles ----------
    p2_angle = NumericProperty(0)
    p3_angle = NumericProperty(0)

    # ---------- State ----------
    selected_amount = NumericProperty(0)
    selected_mode = NumericProperty(2)  # 2 or 3 players

    _stop_polling = False
    _poll_event = None
    _rotate_event = None
    _p2_rotating = BooleanProperty(False)
    _p3_rotating = BooleanProperty(False)
    _pulse_anims = []
    _bot_cache = None
    _last_poll_data = {}
    _popup_timer = None  # kept for safety but NOT USED anymore

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._back_button = None
        Clock.schedule_once(self._ensure_back_button, 0)

    # -------------------------
    # Lifecycle
    # -------------------------
    def on_pre_enter(self, *_):
        if not self._rotate_event:
            self._rotate_event = Clock.schedule_interval(self._rotate_tick, 1 / 60.0)
        Clock.schedule_once(lambda dt: self._apply_pulse_anims(), 0)
        self._ensure_back_button()

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

    def _ensure_back_button(self, *_):
        if self._back_button:
            return
        btn = Button(
            text="Back",
            size_hint=(None, None),
            size=(dp(90), dp(38)),
            pos_hint={"x": 0.02, "y": 0.02},
            background_color=(0.2, 0.2, 0.2, 0.85),
            color=(1, 1, 1, 1),
        )
        btn.bind(on_release=self.go_back_to_stage)
        self.add_widget(btn)
        self._back_button = btn

    def go_back_to_stage(self, *_):
        self._stop_polling = True
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None
        self._stop_pulse_anims()
        self._p2_rotating = False
        self._p3_rotating = False
        if storage and hasattr(storage, "set_current_match"):
            try:
                storage.set_current_match(None)
            except Exception:
                pass
        if self.manager:
            self.manager.current = "stage"

    # -------------------------
    # Start matchmaking
    # -------------------------
    def start_matchmaking(self, local_player_name: str, amount: int, mode: int):
        self.selected_amount = int(amount)
        self.selected_mode = int(mode)
        self._stop_polling = False
        self._bot_cache = None
        self._last_poll_data = {}

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
                        ids_payload = data.get("player_ids")
                        if isinstance(ids_payload, (list, tuple)):
                            storage.set_player_ids(list(ids_payload))
                        else:
                            storage.set_player_ids([data.get("p1_id"), data.get("p2_id"), data.get("p3_id")])
                        idx = self._resolve_my_index_from_payload(data, trusted=True)
                        storage.set_my_player_index(idx if idx is not None else 0)
                        storage.set_current_match(match_id)
                        storage.set_stake_amount(self.selected_amount)
                        storage.set_num_players(self.selected_mode)
                        storage.set_player_names(local_player_name, None, None)
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
    # OFFLINE BOT MODE (ROBOTS Army)
    # -------------------------
    def _fallback_to_bots(self, local_player_name: str):
        BOT_PROFILES = [
            {"id": -1000, "name": "Sharp", "pic": "assets/bot_sharp.png"},
            {"id": -1001, "name": "Crazy Boy", "pic": "assets/bot_crazy.png"},
            {"id": -1002, "name": "Kurfi", "pic": "assets/bot_kurfi.png"},
        ]

        print("[INFO] ROBOTS Army → offline bot mode")

        self._stop_polling = True
        if self._poll_event:
            try:
                self._poll_event.cancel()
            except Exception:
                pass
            self._poll_event = None

        self._bot_cache = {}
        players = [local_player_name]
        ids = [None, None, None]

        if self.selected_mode == 2:
            bot = random.choice(BOT_PROFILES)
            players.append(bot["name"])
            ids[1] = bot["id"]
            self._bot_cache["p2"] = bot
            if "p2_pic" in self.ids:
                self.ids.p2_pic.source = bot["pic"]
            if "p2_name" in self.ids:
                self.ids.p2_name.text = bot["name"]
            self.player3_name = ""
        else:
            bots = random.sample(BOT_PROFILES, 2)
            players.extend([b["name"] for b in bots])
            ids[1] = bots[0]["id"]
            ids[2] = bots[1]["id"]
            self._bot_cache["p2"] = bots[0]
            self._bot_cache["p3"] = bots[1]
            if "p2_pic" in self.ids:
                self.ids.p2_pic.source = bots[0]["pic"]
            if "p2_name" in self.ids:
                self.ids.p2_name.text = bots[0]["name"]
            if "p3_pic" in self.ids:
                self.ids.p3_pic.source = bots[1]["pic"]
            if "p3_name" in self.ids:
                self.ids.p3_name.text = bots[1]["name"]

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

        data = getattr(self, "_last_poll_data", {}) or {}
        if isinstance(ids_or_turn, list):
            pids = ids_or_turn
        else:
            ids_from_payload = data.get("player_ids")
            if isinstance(ids_from_payload, (list, tuple)):
                pids = list(ids_from_payload)
            else:
                pids = [data.get("p1_id"), data.get("p2_id"), data.get("p3_id")]
            turn = int(ids_or_turn) if ids_or_turn is not None else int(data.get("turn") or 0)

        game = self.manager.get_screen("dicegame")
        if self.selected_mode == 2:
            game.set_stage_and_players(self.selected_amount, players[0], players[1], match_id=data.get("match_id"))
        else:
            game.set_stage_and_players(self.selected_amount, players[0], players[1], players[2], match_id=data.get("match_id"))

        if storage:
            storage.set_player_names(*players[: self.selected_mode])
            storage.set_player_ids(pids)

            # Persist my player index for online games so DiceGameScreen knows my slot.
            my_idx = self._resolve_my_index_from_ids(pids)
            if my_idx is None:
                my_idx = self._resolve_my_index_from_payload(data, trusted=True)
            storage.set_my_player_index(my_idx if my_idx is not None else 0)

        Clock.schedule_once(lambda dt: game._place_coins_near_portraits(), 0.15)
        self.manager.current = "dicegame"

        if hasattr(storage, "set_initial_turn"):
            storage.set_initial_turn(int(turn))
        if hasattr(game, "sync_initial_turn"):
            Clock.schedule_once(lambda dt: game.sync_initial_turn(turn), 0.2)
        if hasattr(game, "_screen_ready"):
            try:
                setattr(game, "_screen_ready", False)
                Clock.schedule_once(lambda dt: setattr(game, "_screen_ready", True), 0.3)
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
                if storage:
                    ids_payload = data.get("player_ids")
                    if isinstance(ids_payload, (list, tuple)):
                        storage.set_player_ids(list(ids_payload))
                    else:
                        storage.set_player_ids([data.get("p1_id"), data.get("p2_id"), data.get("p3_id")])
                    idx = self._resolve_my_index_from_payload(data, trusted=True)
                    storage.set_my_player_index(idx if idx is not None else 0)
                if data.get("ready"):
                    self._stop_polling = True
                    if self._poll_event:
                        self._poll_event.cancel()
                        self._poll_event = None
                    self.selected_mode = int(data.get("num_players") or self.selected_mode)
                    players = [data.get("p1") or "Player 1", data.get("p2") or "Player 2"]
                    if self.selected_mode == 3:
                        players.append(data.get("p3") or "Player 3")
                    self._go_game(players, data.get("turn", 0))
        except Exception as e:
            print(f"[ERR] Poll exception: {e}")
