import os
import json
import random
import requests
import threading
import time

from kivy.uix.screenmanager import Screen
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.relativelayout import RelativeLayout
from kivy.uix.behaviors import ButtonBehavior
from kivy.graphics import PushMatrix, PopMatrix, Rotate, Scale
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.animation import Animation
from kivy.factory import Factory
from kivy.uix.popup import Popup
from kivy.uix.boxlayout import BoxLayout
from kivy.properties import StringProperty, NumericProperty, BooleanProperty

API_URL = "https://spin-api-pba3.onrender.com"

try:
    from utils import storage
except Exception:
    storage = None

try:
    import websocket  # type: ignore
    WEBSOCKET_OK = True
except Exception:
    WEBSOCKET_OK = False


# ------------------------
# Polygon Dice widget
# ------------------------
class PolygonDice(ButtonBehavior, RelativeLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rotation_angle = 0
        self._anim = None
        self._dice_image = Image(
            source="assets/dice/dice1.png",
            allow_stretch=True,
            keep_ratio=True,
        )
        self.add_widget(self._dice_image)

        with self.canvas.before:
            PushMatrix()
            self._rot = Rotate(angle=0, origin=self.center)
            self._scale = Scale(1, 1, 1, origin=self.center)
        with self.canvas.after:
            PopMatrix()

        self.bind(pos=self._update_transform, size=self._update_transform, center=self._update_transform)

    def _update_transform(self, *_):
        if hasattr(self, "_rot"):
            self._rot.origin = self.center
        if hasattr(self, "_scale"):
            self._scale.origin = self.center

    def animate_spin(self, result: int):
        """Animate dice spin, then set the final face image."""
        self.stop_spin()

        spin_seq = (
            Animation(_rotation_angle=360, d=0.25, t="linear")
            + Animation(_rotation_angle=720, d=0.25, t="linear")
            + Animation(_rotation_angle=540, d=0.25, t="linear")
        )

        zoom = (
            Animation(scale_value=1.2, d=0.15, t="out_quad")
            + Animation(scale_value=1.0, d=0.2, t="out_quad")
        )

        spin_seq.bind(on_progress=lambda *_: self._sync_rotation())
        zoom.start(self)
        spin_seq.start(self)
        self._anim = spin_seq

        def set_final_face(*_):
            img_path = os.path.join("assets", "dice", f"dice{result}.png")
            if os.path.exists(img_path):
                self._dice_image.source = img_path
                self._dice_image.reload()

            self.stop_spin()
            self._rotation_angle = 0
            self._sync_rotation()

        spin_seq.bind(on_complete=set_final_face)

    def stop_spin(self):
        if self._anim:
            self._anim.stop(self)
            self._anim = None

    def _sync_rotation(self, *_):
        if hasattr(self, "_rot"):
            self._rot.angle = self._rotation_angle

    @property
    def scale_value(self):
        return self._scale.x

    @scale_value.setter
    def scale_value(self, value):
        self._scale.x = self._scale.y = value


# register for KV
Factory.register("PolygonDice", cls=PolygonDice)


# ------------------------
# Dice Game Screen
# ------------------------
class DiceGameScreen(Screen):
    dice_result = StringProperty("")
    stage_amount = NumericProperty(0)
    stage_label = StringProperty("Free Play")

    player1_name = StringProperty("Player 1")
    player2_name = StringProperty("Player 2")
    player3_name = StringProperty("")

    _current_player = NumericProperty(0)
    _game_active = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._positions = [0, 0, 0]
        self._coins = [None, None, None]
        self._spawned_on_board = [False, False, False]
        self._winner_shown = False
        self._num_players = 2

        # online sync
        self._online = False
        self._ws = None
        self._ws_thread = None
        self._ws_stop = threading.Event()
        self._poll_ev = None
        self.match_id = None

        # state helpers
        self._my_index = None
        self._roll_inflight = False
        self._last_roll_seen = None
        self._last_state_sig = None  # (positions tuple, last_roll, turn)
        self._last_roll_animated = None
        self._forfeited_players = set()
        self._first_turn_synced = False
        self._auto_from_timer = False
        self._last_roll_time = 0

    # ---------- helpers ----------
    def _root_float(self):
        """Overlay for coins."""
        lyr = self.ids.get("coin_layer")
        return lyr if lyr else self

    def _add_on_top(self, w):
        parent = self._root_float()
        if w.parent is not None and w.parent is not parent:
            try:
                w.parent.remove_widget(w)
            except Exception:
                pass
        if w.parent is None:
            parent.add_widget(w)
        else:
            try:
                w.parent.remove_widget(w)
            except Exception:
                pass
            parent.add_widget(w)

    def _bring_coins_to_front(self):
        for c in self._coins:
            if c:
                self._add_on_top(c)

    def _map_center_to_parent(self, target_parent, widget):
        try:
            wx, wy = widget.to_window(widget.center_x, widget.center_y)
            px, py = target_parent.to_widget(wx, wy, relative=True)
            return px, py
        except Exception:
            return widget.center

    def _backend(self):
        if storage and storage.get_backend_url():
            return storage.get_backend_url()
        return API_URL

    def _token(self):
        return storage.get_token() if storage else None

    def _debug(self, msg: str):
        try:
            print(msg)
        except Exception:
            pass

    def _set_dice_button_enabled(self, enabled: bool):
        btn = self.ids.get("dice_button")
        if not btn:
            return
        try:
            btn.disabled = not enabled
            btn.opacity = 1.0 if enabled else 0.6
        except Exception:
            pass

    def _mark_roll_start(self, source: str = "manual"):
        self._roll_inflight = True
        self._roll_locked = True
        self._roll_source = source
        self._set_dice_button_enabled(False)

    def _mark_roll_end(self):
        self._roll_inflight = False
        self._roll_locked = False
        self._roll_source = None
        self._set_dice_button_enabled(True)

    # ---------- lifecycle ----------
    def on_pre_enter(self, *_):
        if storage:
            try:
                players = list(storage.get_player_names() or (None, None, None))
                while len(players) < 3:
                    players.append(None)
                stake = storage.get_stake_amount()
                self._num_players = storage.get_num_players() or 2
                self.player1_name = players[0] or (storage.get_user().get("name") if storage.get_user() else "You")
                self.player2_name = players[1] or "Bot"
                self.player3_name = players[2] if self._num_players == 3 else ""
                if stake is not None:
                    self.stage_amount = int(stake)
                    self.stage_label = "Free Play" if stake == 0 else f"₹{stake} Bounty"
            except Exception as e:
                print(f"[WARN] Failed loading storage: {e}")

        Clock.schedule_once(lambda dt: self._apply_initial_portraits(), 0)
        Clock.schedule_once(lambda dt: self._place_coins_near_portraits(), 0.05)
        mid = storage.get_current_match() if storage else None

        # bot vs online
        player2 = self.player2_name.lower().strip() if self.player2_name else ""
        bot_names = ["bot", "crazy boy", "kurfi", "sharp"]

        if player2 in bot_names or (not mid):
            self._online = False
            self.match_id = None
            if storage:
                try:
                    storage.set_current_match(None)
                except Exception:
                    pass
            self._debug("[MODE] Bot/Offline mode forced (no server sync)")
            self._game_active = True
            self._current_player = 0
            self._highlight_turn()
        else:
            self._online = True
            self.match_id = mid
            self._debug(f"[MODE] Online match detected (ID={mid})")
            self._game_active = True
            self._start_online_sync()

    def on_leave(self, *_):
        self._stop_online_sync()

    # ---------- portraits ----------
    def _resolve_avatar_source(self, index: int, name: str, pid):
        bot_map_by_id = {
            -1000: "assets/bot_sharp.png",
            -1001: "assets/bot_crazy.png",
            -1002: "assets/bot_kurfi.png",
        }
        bot_map_by_name = {
            "sharp": "assets/bot_sharp.png",
            "crazy boy": "assets/bot_crazy.png",
            "kurfi": "assets/bot_kurfi.png",
        }
        if isinstance(pid, int) and pid in bot_map_by_id:
            return bot_map_by_id[pid]
        n = (name or "").strip().lower()
        if n in bot_map_by_name:
            return bot_map_by_name[n]
        return "assets/default.png"

    def _apply_initial_portraits(self):
        pids = storage.get_player_ids() if storage else [None, None, None]
        names = [self.player1_name, self.player2_name, self.player3_name]

        if "p1_pic" in self.ids:
            self.ids.p1_pic.source = self._resolve_avatar_source(0, names[0], pids[0])

        if "p2_pic" in self.ids:
            self.ids.p2_pic.source = self._resolve_avatar_source(1, names[1], pids[1])

        if self.player3_name and "p3_pic" in self.ids:
            self.ids.p3_pic.source = self._resolve_avatar_source(2, names[2], pids[2])

    # ---------- state ----------
    def _reset_game_state(self):
        """Reset local positions and flags; used mainly for offline/bot games."""
        self._positions = [0, 0, 0]
        self._spawned_on_board = [False, False, False]
        self.dice_result = ""
        self._winner_shown = False
        self._game_active = True
        if hasattr(self, "_forfeited_players"):
            self._forfeited_players.clear()
        else:
            self._forfeited_players = set()

        # reset volatile flags for new sessions to avoid stale turn/lock state
        self._first_turn_synced = False
        self._auto_from_timer = False
        self._roll_inflight = False
        self._roll_locked = False
        self._last_roll_time = 0

        # restore any portraits/coins that might have been hidden due to previous forfeits
        for idx in range(3):
            pic = self.ids.get(f"p{idx + 1}_pic")
            if pic:
                pic.opacity = 1
            if idx < len(self._coins) and self._coins[idx]:
                self._coins[idx].opacity = 1

        forfeited = getattr(self, "_forfeited_players", set())
        active_players = [i for i in range(self._num_players) if i not in forfeited]
        if not active_players:
            self._debug("[RESET] All players forfeited — stopping game.")
            self._game_active = False
            return

        # start turn handling
        if self._online:
            self._current_player = -1  # wait for backend sync
            self._set_dice_button_enabled(False)
        else:
            try:
                self._current_player = random.choice(active_players)
            except Exception:
                self._current_player = active_players[0]
        self._place_coins_near_portraits()
        self._highlight_turn()

    def set_stage_and_players(self, amount: int, p1: str, p2: str, p3: str = None, match_id=None):
        self.stage_amount = amount
        self.player1_name = p1 or "Player 1"
        self.player2_name = p2 or "Player 2"
        self.player3_name = p3 or ""
        self.stage_label = "Free Play" if amount == 0 else f"₹{amount} Bounty"
        self.match_id = match_id
        self._online = bool(match_id)
        self._resolve_my_index()
        self._reset_game_state()

        if self.player2_name.lower().strip() in ["bot", "crazy boy", "kurfi", "sharp"]:
            self._online = False
            self.match_id = None
            if storage:
                try:
                    storage.set_current_match(None)
                except Exception:
                    pass
            self._debug("[INIT] Forcing offline mode for bot game")

        Clock.schedule_once(lambda dt: self._apply_initial_portraits(), 0)
        Clock.schedule_once(lambda dt: self._place_coins_near_portraits(), 0.05)
        if self._online:
            self._start_online_sync()
        else:
            self._highlight_turn()

    def _resolve_my_index(self):
        fallback_idx = 0 if not self._online else None
        try:
            # Prefer stored slot if matchmaking recorded it.
            stored_idx = storage.get_my_player_index() if storage else None
            num_players = storage.get_num_players() if storage else self._num_players
            if isinstance(stored_idx, int) and 0 <= stored_idx < (num_players or self._num_players):
                self._my_index = stored_idx
                return

            user = storage.get_user() if storage else None
            uid = None
            if isinstance(user, dict):
                uid = user.get("id") or user.get("_id")

            pids = storage.get_player_ids() if storage else []
            num = num_players or self._num_players
            if uid is not None:
                for i, pid in enumerate((pids or [])[:num]):
                    if pid is not None and str(pid) == str(uid):
                        self._my_index = i
                        if storage:
                            storage.set_my_player_index(i)
                        return

        except Exception as e:
            print(f"[INDEX][WARN] resolve failed: {e}")
            fallback_idx = 0

        self._my_index = fallback_idx
        if fallback_idx is not None and storage:
            storage.set_my_player_index(fallback_idx)

    def _maybe_update_my_index_from_payload(self, payload, trusted: bool = False):
        """Try to refresh my_index based on server payload."""
        if not payload:
            return False

        idx = None
        my_uid = None
        if storage:
            user = storage.get_user() or {}
            my_uid = user.get("id") or user.get("_id")

        candidates = payload.get("player_ids") or payload.get("players_ids") or payload.get("ids")
        if my_uid is not None and isinstance(candidates, (list, tuple)):
            for i, pid in enumerate(candidates):
                if pid is not None and str(pid) == str(my_uid):
                    idx = i
                    break

        if idx is None and trusted:
            for key in ("player_index", "player_idx", "my_index", "self_index"):
                if key in payload and payload.get(key) is not None:
                    idx = payload.get(key)
                    break

        if idx is None:
            return False

        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return False

        self._my_index = idx
        if storage:
            storage.set_my_player_index(idx)
        return True

    # ---------- turn ----------
    def _highlight_turn(self):
        """Highlight current player and handle bot/idle timers in offline mode."""
        p1_overlay = self.ids.get("p1_overlay")
        p2_overlay = self.ids.get("p2_overlay")
        p3_overlay = self.ids.get("p3_overlay")

        def pulse(widget, active: bool):
            if not widget:
                return
            Animation.cancel_all(widget, "opacity")
            if active:
                anim = (
                    Animation(opacity=0.6, d=1.0, t="in_out_quad")
                    + Animation(opacity=0.2, d=1.0, t="in_out_quad")
                )
                anim.repeat = True
                anim.start(widget)
            else:
                widget.opacity = 0

        pulse(p1_overlay, self._current_player == 0)
        pulse(p2_overlay, self._current_player == 1)
        pulse(p3_overlay, self._current_player == 2 and bool(self.player3_name))

        if self._online:
            self._debug(f"[TURN][UI] Online turn highlight for player {self._current_player}")
            self._set_dice_button_enabled(self._current_player == self._my_index and self._current_player >= 0)
            return

        # offline
        if self._current_player != 0:
            self._debug(f"[BOT TURN] Player {self._current_player} auto-roll soon")
            Clock.schedule_once(lambda dt: self._auto_roll_current(), 0.3)
        else:
            self._debug("[TIMER] Offline player idle → auto-roll in 10s")
            self._cancel_turn_timer()
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

    # ---------- dice ----------
    def roll_dice(self):
        """Entry point from UI (click) or timers."""
        if not self._game_active:
            return

        if getattr(self, "_roll_inflight", False):
            self._debug("[ROLL] Blocked — roll already processing.")
            return

        # cancel any pending auto-roll timer as soon as a roll is initiated manually/externally
        self._cancel_turn_timer()

        now = time.time()
        if hasattr(self, "_last_roll_time") and now - getattr(self, "_last_roll_time", 0) < 1.5:
            self._debug("[ROLL] Ignoring duplicate roll trigger within 1.5s window.")
            return
        self._last_roll_time = now

        if not self._online:
            if self._current_player != 0:
                self._show_temp_popup("Not your turn!", duration=1.8)
                return

            self._mark_roll_start("offline_manual")
            roll = random.randint(1, 6)
            if "dice_button" in self.ids:
                self.ids.dice_button.animate_spin(roll)
            Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)
            Clock.schedule_once(lambda dt: self._mark_roll_end(), 1.0)
            return

        # ONLINE
        if not self.match_id or not self._token():
            self._debug("[ROLL] Missing match_id or token — aborting online roll.")
            return

        if self._my_index is None:
            self._debug("[ROLL] Waiting for player index sync before rolling.")
            self._sync_remote_turn("no-player-index")
            return

        if not getattr(self, "_first_turn_synced", False):
            self._first_turn_synced = True
            try:
                resp = requests.get(
                    f"{self._backend()}/matches/check",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    params={"match_id": self.match_id},
                    timeout=5,
                    verify=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._maybe_update_my_index_from_payload(data)
                    srv_turn = data.get("turn")
                    if srv_turn is not None:
                        self._current_player = int(srv_turn)
            except Exception as e:
                self._debug(f"[TURN][SYNC][ERR] {e}")

        if self._current_player != self._my_index:
            if not getattr(self, "_auto_from_timer", False):
                self._show_temp_popup("Not your turn!", duration=1.8)
            return

        # final verification with backend to avoid stale turn state
        try:
            resp = requests.get(
                f"{self._backend()}/matches/check",
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"match_id": self.match_id},
                timeout=5,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._maybe_update_my_index_from_payload(data, trusted=True)
                srv_turn = data.get("turn")
                if srv_turn is not None:
                    self._current_player = int(srv_turn)
                if self._current_player != self._my_index:
                    self._debug("[ROLL] Aborted — backend reports different turn.")
                    self._mark_roll_end()
                    if not getattr(self, "_auto_from_timer", False):
                        self._show_temp_popup("Not your turn!", duration=1.5)
                    return
        except Exception as e:
            self._debug(f"[ROLL][VERIFY][ERR] {e}")
            # fallback to previous state; continue rolling

        source = "online_auto" if getattr(self, "_auto_from_timer", False) else "online_manual"
        self._mark_roll_start(source)

        def worker():
            try:
                resp = requests.post(
                    f"{self._backend()}/matches/roll",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    json={"match_id": self.match_id},
                    timeout=8,
                    verify=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    roll_val = int(data.get("roll") or 1)
                    Clock.schedule_once(
                        lambda dt: self._animate_dice_and_apply_server(data, roll_val), 0
                    )

                elif resp.status_code == 409:
                    self._debug("[TURN] Server rejected roll — not your turn.")
                    if not getattr(self, "_auto_from_timer", False):
                        self._show_temp_popup("Not your turn!", duration=1.5)
                    Clock.schedule_once(lambda dt: self._mark_roll_end(), 0)
                    Clock.schedule_once(lambda dt: setattr(self, "_last_roll_time", 0), 0)
                    Clock.schedule_once(lambda dt: self._sync_remote_turn("409"), 0)
                    return

                elif resp.status_code == 400 and "Match not active" in resp.text:
                    self._debug("[ROLL] Match not active — stopping game & timers.")
                    self._game_active = False
                    self._cancel_turn_timer()
                    self._mark_roll_end()
                    return

                else:
                    self._debug(f"[ROLL][HTTP] Unexpected {resp.status_code}: {resp.text}")

            except Exception as e:
                self._debug(f"[ROLL][ERR] {e}")

            finally:
                Clock.schedule_once(lambda dt: self._mark_roll_end(), 0.1)
                Clock.schedule_once(lambda dt: setattr(self, "_last_roll_time", 0), 0.1)
                if getattr(self, "_auto_from_timer", False):
                    Clock.schedule_once(lambda dt: setattr(self, "_auto_from_timer", False), 0)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- offline roll core ----------
    def _apply_roll(self, roll: int):
        """
        Offline dice roll logic — mirrors backend rules:
          - Spawn: only when rolling 1 (if not spawned yet).
          - Box 3: danger → coin returns to box 0.
          - Overshoot >7: stay where you are.
          - Exact 7: win.
          - Capture: if you land on opponent box, opponent goes back to box 0.
          - Turn order: ALWAYS p0 → p1 → p2 → p0 → ... (no extra turns).
        """
        if self._online:
            self._debug("[SKIP] Online mode active — backend handles dice roll.")
            return

        p = self._current_player
        old = self._positions[p]
        new_pos = old + roll
        BOARD_MAX = 7
        DANGER_BOX = 3

        self._debug(f"[OFFLINE] Player {p} rolled {roll} (from {old})")

        # --- Rule 1: Spawn only when rolling 1 ---
        if not self._spawned_on_board[p]:
            if roll == 1:
                self._spawned_on_board[p] = True
                self._positions[p] = 0
                self._move_coin_to_box(p, 0)
                self._debug(f"[SPAWN] Player {p} enters at box 0")
            else:
                self._debug(f"[SKIP] Player {p} not spawned (roll={roll})")
            Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.5)
            return

        # --- Rule 2: Danger zone (box 3 → reset to 0) ---
        if new_pos == DANGER_BOX:
            self._debug(f"[DANGER] Player {p} hit box 3 → reset to start")
            self._positions[p] = 0
            # optional: show brief move to 3 then back to 0 using reverse flag
            self._move_coin_to_box(p, DANGER_BOX)
            self._game_active = False

            def do_reverse_reset(*_):
                self._move_coin_to_box(p, 0, reverse=True)
                self._debug(f"[RESET] Player {p} safely returned to start")
                self._game_active = True
                self._end_turn_and_highlight()

            Clock.schedule_once(do_reverse_reset, 0.8)
            return

        # --- Rule 3: Win condition (==7) ---
        if new_pos == BOARD_MAX:
            self._positions[p] = BOARD_MAX
            self._move_coin_to_box(p, BOARD_MAX)
            self._declare_winner(p)
            return

        # --- Rule 4: Overshoot (>7) → stay on current box ---
        if new_pos > BOARD_MAX:
            self._debug(f"[OVERSHOOT] Player {p} rolled {roll} → stays at {old}")
            self._positions[p] = old
            self._move_coin_to_box(p, old)
            Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.5)
            return

        # --- Rule 5: Normal move ---
        self._positions[p] = new_pos
        self._move_coin_to_box(p, new_pos)
        self._debug(f"[MOVE] Player {p} moved to box {new_pos}")

        # --- Rule 6: Capture — if land on opponent, send them to 0 ---
        for idx in range(self._num_players):
            if idx == p:
                continue
            # only capture coins that have actually spawned
            if self._spawned_on_board[idx] and self._positions[idx] == self._positions[p]:
                self._debug(f"[CAPTURE] Player {p} captures player {idx} at box {new_pos} → player {idx} back to 0")
                self._positions[idx] = 0
                self._move_coin_to_box(idx, 0)

        Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.6)

    def _end_turn_and_highlight(self):
        """Advance strictly +1 turn in offline mode."""
        if getattr(self, "_end_turn_pending", False):
            self._debug("[TURN] End-turn already pending — skipping duplicate.")
            return
        self._end_turn_pending = True

        self._roll_locked = False
        self._current_player = (self._current_player + 1) % self._num_players
        self._debug(f"[TURN] Switching → player {self._current_player}")

        def finish_turn(*_):
            self._end_turn_pending = False
            self._highlight_turn()
            self._start_turn_timer()

        Clock.schedule_once(finish_turn, 0.3)

    # ---------- victory ----------
    def _declare_winner(self, winner_idx: int):
        if self._winner_shown:
            return
        self._winner_shown = True
        self._game_active = False

        names = [self.player1_name, self.player2_name, self.player3_name]
        name = names[winner_idx] if winner_idx < len(names) else "Player"

        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text=f"{name} wins!", halign="center"))

        popup = Popup(
            title="Victory",
            content=layout,
            size_hint=(None, None),
            size=(300, 200),
            auto_dismiss=True,
        )
        popup.open()

        def close_popup_and_reset(*_):
            try:
                popup.dismiss()
            except Exception:
                pass
            self._reset_after_popup()

        Clock.schedule_once(close_popup_and_reset, 2.5)

    def _reset_after_popup(self, *_):
        self._stop_online_sync()
        self._game_active = False
        self._winner_shown = False
        if self.manager:
            self.manager.current = "stage"

    # ---------- forfeit ----------
    def force_player_exit(self):
        if getattr(self, "_forfeit_lock", False):
            self._debug("[FORFEIT] Click ignored (lock active).")
            return
        self._forfeit_lock = True
        Clock.schedule_once(lambda dt: setattr(self, "_forfeit_lock", False), 3.0)

        self._debug("[FORFEIT] Give Up pressed.")
        backend, token, match_id = self._backend(), self._token(), self.match_id
        self._game_active = False

        if not (backend and token and match_id):
            self._show_forfeit_popup("You gave up! Opponent wins.")
            Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)
            return

        def worker():
            try:
                self._debug(f"[FORFEIT] Sending request to backend for match {match_id}")
                resp = requests.post(
                    f"{backend}/matches/forfeit",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"match_id": match_id},
                    timeout=10,
                    verify=False,
                )
                data = resp.json() if resp.status_code == 200 else {}

                if resp.status_code == 400 and "already finished" in resp.text.lower():
                    self._debug("[FORFEIT] Match already finished — skipping.")
                    Clock.schedule_once(lambda dt: self._reset_after_popup(), 1.5)
                    return

                if data.get("continuing"):
                    Clock.schedule_once(
                        lambda dt: self._show_forfeit_popup("You gave up! Others continue playing."), 0
                    )
                    Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)
                    return

                winner = data.get("winner_name", "Opponent")
                Clock.schedule_once(
                    lambda dt: self._show_forfeit_popup(f"You gave up! {winner} wins."), 0
                )
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)

            except Exception as e:
                self._debug(f"[FORFEIT][ERR] {e}")
                Clock.schedule_once(
                    lambda dt: self._show_forfeit_popup("You gave up! Opponent wins."), 0
                )
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)

        threading.Thread(target=worker, daemon=True).start()

    def _show_forfeit_popup(self, msg: str):
        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text=msg, halign="center"))

        popup = Popup(
            title="Notice",
            content=layout,
            size_hint=(None, None),
            size=(300, 200),
            auto_dismiss=True,
        )
        popup.open()

        def close_popup_and_reset(*_):
            try:
                popup.dismiss()
            except Exception:
                pass
            self._reset_after_popup()

        Clock.schedule_once(close_popup_and_reset, 2.5)

    # ---------- toast ----------
    def _show_temp_popup(self, msg: str, duration: float = 2.0):
        # ensure popup interactions always run on the main/UI thread
        if threading.current_thread() is not threading.main_thread():
            Clock.schedule_once(lambda dt: self._show_temp_popup(msg, duration), 0)
            return

        try:
            if not hasattr(self, "_toast_popup") or self._toast_popup is None:
                layout = BoxLayout(orientation="vertical", spacing=8, padding=(14, 12, 14, 12))
                self._toast_label = Label(text=msg, halign="center", valign="middle")
                self._toast_label.bind(
                    size=lambda *_: setattr(self._toast_label, "text_size", self._toast_label.size)
                )
                layout.add_widget(self._toast_label)
                self._toast_popup = Popup(
                    title="",
                    content=layout,
                    size_hint=(None, None),
                    size=(300, 140),
                    auto_dismiss=True,
                    separator_height=0,
                    background_color=(0, 0, 0, 0.8),
                )

            self._toast_label.text = msg
            if not self._toast_popup.parent:
                self._toast_popup.open()

            if hasattr(self, "_toast_ev") and self._toast_ev:
                try:
                    self._toast_ev.cancel()
                except Exception:
                    pass

            def _close(*_):
                try:
                    if self._toast_popup:
                        self._toast_popup.dismiss()
                except Exception:
                    pass
                self._toast_ev = None

            self._toast_ev = Clock.schedule_once(_close, max(1.5, float(duration)))

        except Exception as e:
            self._debug(f"[TOAST][ERR] {e}")

    def _animate_dice_and_apply_server(self, data, roll):
        if "dice_button" in self.ids:
            self.ids.dice_button.animate_spin(roll)
        Clock.schedule_once(lambda dt: self._on_server_event(data), 0.8)

    # ---------- coins ----------
    def _ensure_coin_widgets(self):
        layer = self._root_float()

        def ensure(idx, src):
            if self._coins[idx] is None:
                self._coins[idx] = Image(source=src, size_hint=(None, None), opacity=1)
                layer.add_widget(self._coins[idx])
            elif self._coins[idx].parent is not layer:
                try:
                    if self._coins[idx].parent:
                        self._coins[idx].parent.remove_widget(self._coins[idx])
                except Exception:
                    pass
                layer.add_widget(self._coins[idx])

        ensure(0, "assets/coins/red.png")
        ensure(1, "assets/coins/yellow.png")
        if self.player3_name:
            ensure(2, "assets/coins/green.png")

        self._bring_coins_to_front()

    def _place_coins_near_portraits(self):
        self._ensure_coin_widgets()
        layer = self._root_float()

        def place(pic_id, coin_img, idx):
            pic = self.ids.get(pic_id)
            if not (coin_img and pic):
                return
            cx, cy = self._map_center_to_parent(layer, pic)
            coin_img.size = (dp(24), dp(24))
            coin_img.center = (cx, cy - dp(50))
            coin_img.opacity = 1

        place("p1_pic", self._coins[0], 0)
        place("p2_pic", self._coins[1], 1)
        if self._num_players == 3:
            place("p3_pic", self._coins[2], 2)

    def _move_coin_to_box(self, idx: int, pos: int, reverse=False):
        box = self.ids.get(f"box_{pos}")
        coin = self._coins[idx] if idx < len(self._coins) else None
        if not box or not coin:
            return

        Animation.cancel_all(coin)

        h = getattr(box, "height", dp(50))
        size_px = max(dp(40), min(dp(64), h * 0.9))
        coin.size = (size_px, size_px)

        layer = self._root_float()
        offsets = {0: -dp(14), 1: 0, 2: dp(14)}
        stack_x = offsets.get(idx, 0)
        stack_y = 0

        if reverse:
            start_anchor = self.ids.get("box_0")
            if start_anchor:
                tx, ty = self._map_center_to_parent(layer, start_anchor)
                Animation(center=(tx, ty), d=0.5, t="out_quad").start(coin)
                self._positions[idx] = 0
                self._debug(f"[REVERSE] Player {idx} reset to start box.")
            else:
                self._debug("[WARN] box_0 missing; reverse skipped.")
            return

        tx, ty = self._map_center_to_parent(layer, box)
        Animation(center=(tx + stack_x, ty + stack_y), d=0.5, t="out_quad").start(coin)
        self._positions[idx] = pos
        self._debug(f"[MOVE] Player {idx} now at {pos}")

    def _apply_positions_to_board(self, positions, reverse=False):
        self._ensure_coin_widgets()
        self._num_players = 3 if (len(positions) >= 3 and self.player3_name) else 2
        for idx in range(self._num_players):
            self._positions[idx] = int(positions[idx])
            self._move_coin_to_box(idx, int(positions[idx]), reverse=False)

    # ---------- ONLINE sync ----------
    def _start_online_sync(self):
        self._stop_online_sync()
        self._resolve_my_index()
        self._last_roll_seen = None
        self._last_state_sig = None
        self._last_roll_animated = None
        self._first_turn_synced = False
        if WEBSOCKET_OK:
            self._ws_stop.clear()
            self._ws_thread = threading.Thread(target=self._ws_worker, daemon=True)
            self._ws_thread.start()
        else:
            self._poll_ev = Clock.schedule_interval(lambda dt: self._poll_state_once(), 0.9)
        # ensure we have the latest state immediately
        self._sync_remote_turn("start-sync", trusted=True)

    def _stop_online_sync(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        if self._ws_thread:
            self._ws_stop.set()
            self._ws_thread = None
        if self._poll_ev:
            try:
                self._poll_ev.cancel()
            except Exception:
                pass
            self._poll_ev = None

    def _ws_worker(self):
        url = self._backend().replace("http", "ws") + f"/matches/ws/{self.match_id}"
        headers = [f"Authorization: Bearer {self._token()}"] if self._token() else []

        def on_message(ws, message):
            try:
                payload = json.loads(message)
                Clock.schedule_once(lambda dt: self._on_server_event(payload), 0)
            except Exception:
                pass

        self._ws = websocket.WebSocketApp(url, header=headers, on_message=on_message)
        while not self._ws_stop.is_set():
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            if self._ws_stop.wait(2.0):
                break

    def _poll_state_once(self):
        try:
            resp = requests.get(
                f"{self._backend()}/matches/check",
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"match_id": self.match_id},
                timeout=8,
                verify=False,
            )
            if resp.status_code == 200:
                self._on_server_event(resp.json())
            elif resp.status_code == 404:
                self._stop_online_sync()
        except Exception as e:
            self._debug(f"[POLL][ERR] {e}")

    def _sync_remote_turn(self, reason: str = "", trusted: bool = False):
        """Force-refresh state from backend (used after 409 or manual resync)."""
        if not self._online or not self.match_id or not self._token():
            return

        self._debug(f"[SYNC][REFRESH] Triggered ({reason or 'unspecified'})")

        def worker():
            try:
                resp = requests.get(
                    f"{self._backend()}/matches/check",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    params={"match_id": self.match_id},
                    timeout=6,
                    verify=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._maybe_update_my_index_from_payload(data, trusted=trusted)
                    Clock.schedule_once(lambda dt: self._on_server_event(data), 0)
            except Exception as e:
                self._debug(f"[SYNC][REFRESH][ERR] {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- core server event handler ----------
    def _on_server_event(self, payload: dict):
        try:
            self._maybe_update_my_index_from_payload(payload)

            # =====================================================================
            # 0. UNIVERSAL FINISH CATCH (works for WIN + FORFEIT)
            # =====================================================================
            if payload.get("finished") is True or payload.get("status") == "FINISHED":
                self._debug("[SYNC] FINISHED flag detected → stopping game.")
                self._game_active = False
                self._cancel_turn_timer()

                winner = payload.get("winner")
                my_idx = getattr(self, "_my_index", None)

                # I AM WINNER
                if winner is not None and my_idx is not None and int(winner) == int(my_idx):
                    self._debug("[SYNC] I am winner → declare popup")
                    Clock.schedule_once(lambda dt: self._declare_winner(int(winner)), 0.5)
                    return

                # I AM LOSER
                if winner is not None and my_idx is not None and int(winner) != int(my_idx):
                    self._debug("[SYNC] I lost → popup + redirect")
                    self._show_temp_popup("You Lost!", duration=1.5)
                    self._stop_online_sync()
                    if self.manager:
                        Clock.schedule_once(lambda dt: setattr(self.manager, "current", "stage"), 1.6)
                    return

                # FALLBACK — my_idx IS NONE (critical fix)
                self._debug("[SYNC] FINISHED but my_idx is None → fallback redirect")
                self._stop_online_sync()
                if self.manager:
                    Clock.schedule_once(lambda dt: setattr(self.manager, "current", "stage"), 1.0)
                return

            # =====================================================================
            # 1. LEGACY STATUS FINISHED (keep this for safety)
            # =====================================================================
            if payload.get("status") == "FINISHED":
                self._debug("[SYNC] Backend says FINISHED (legacy mode)")
                self._game_active = False
                self._cancel_turn_timer()
                backend_winner = payload.get("winner")
                my_idx = getattr(self, "_my_index", None)

                if backend_winner is not None and my_idx is not None:
                    if int(backend_winner) != int(my_idx):
                        self._debug("[SYNC] Legacy FINISHED → I lost")
                        self._show_temp_popup("You Lost!", duration=1.5)
                        self._stop_online_sync()
                        if self.manager:
                            Clock.schedule_once(
                                lambda dt: setattr(self.manager, "current", "stage"),
                                1.6,
                            )
                        return
                return

            # =====================================================================
            # 2. Extract common state
            # =====================================================================
            winner = payload.get("winner")
            positions = payload.get("positions") or self._positions
            roll = payload.get("last_roll")
            actor = payload.get("actor")
            turn = payload.get("turn")
            spawn = payload.get("spawn", False)
            forfeit_actor = payload.get("forfeit_actor")

            # =====================================================================
            # 3. Duplicate filter
            # =====================================================================
            sig = (tuple(positions), int(roll or 0), int(turn or -1))
            if getattr(self, "_last_state_sig", None) == sig:
                self._debug("[SYNC] Duplicate state – ignored")
                return
            self._last_state_sig = sig

            # =====================================================================
            # 4. Dice animation
            # =====================================================================
            if roll and "dice_button" in self.ids:
                try:
                    self.ids.dice_button.animate_spin(int(roll))
                except:
                    pass

            # =====================================================================
            # 5. Spawn handler
            # =====================================================================
            if spawn and actor is not None:
                self._debug(f"[SPAWN] Player {actor} enters board at 0")
                self._spawned_on_board[actor] = True
                self._move_coin_to_box(actor, 0)

                # Turn update
                self._current_player = int(turn) if turn is not None else (actor + 1) % self._num_players
                self._debug(f"[TURN][SPAWN] → player {self._current_player}")

                Clock.schedule_once(lambda dt: self._unlock_and_continue(), 0.6)
                return

            # =====================================================================
            # 6. Move handling
            # =====================================================================
            old_positions = self._positions[:]
            self._positions = [int(p) for p in positions]
            self._ensure_coin_widgets()

            for i, (a, b) in enumerate(zip(old_positions, positions)):
                if a != b:
                    self._move_coin_to_box(i, b)
                    self._debug(f"[MOVE] Player {i}: {a} → {b}")

            # =====================================================================
            # 7. FORFEIT HANDLING
            # =====================================================================
            if forfeit_actor is not None:
                if not hasattr(self, "_forfeited_players"):
                    self._forfeited_players = set()

                self._forfeited_players.add(forfeit_actor)

                # Hide UI
                if f"p{forfeit_actor + 1}_pic" in self.ids:
                    self.ids[f"p{forfeit_actor + 1}_pic"].opacity = 0
                if forfeit_actor < len(self._coins) and self._coins[forfeit_actor]:
                    self._coins[forfeit_actor].opacity = 0

                self._debug(f"[FORFEIT] Player {forfeit_actor} removed")
                self._show_temp_popup(f"Player {forfeit_actor + 1} gave up!", duration=2)

                # Active players
                active = [i for i in range(self._num_players) if i not in self._forfeited_players]

                # 🔥🔥 If only ONE player remains → declare auto-win (frontend)
                if len(active) == 1:
                    my_idx = getattr(self, "_my_index", None)

                    if my_idx is not None and active[0] == my_idx:
                        self._debug("[FORFEIT] I am the last player → auto-win popup")
                        self._cancel_turn_timer()
                        self._stop_online_sync()
                        Clock.schedule_once(lambda dt: self._declare_winner(my_idx), 0.6)
                        return

                    # If I am not the last
                    self._debug("[FORFEIT] I am NOT the last → auto-loss redirect")
                    self._cancel_turn_timer()
                    self._stop_online_sync()
                    self._show_temp_popup("You Lost!", duration=1.5)
                    if self.manager:
                        Clock.schedule_once(lambda dt: setattr(self.manager, "current", "stage"), 1.6)
                    return

                # More than one active player → continue
                self._current_player = active[0]
                self._unlock_and_continue()
                return

            # =====================================================================
            # 8. WINNER HANDLING (normal win)
            # =====================================================================
            if winner is not None:
                my_idx = getattr(self, "_my_index", None)
                self._debug(f"[WINNER] Player {winner}")

                if my_idx is not None and int(winner) == int(my_idx):
                    self._cancel_turn_timer()
                    Clock.schedule_once(lambda dt: self._declare_winner(int(winner)), 0.7)
                    return

                if my_idx is not None and int(winner) != int(my_idx):
                    self._game_active = False
                    self._cancel_turn_timer()
                    self._show_temp_popup("You Lost!", duration=1.5)
                    self._stop_online_sync()
                    if self.manager:
                        Clock.schedule_once(lambda dt: setattr(self.manager, "current", "stage"), 1.6)
                    return

                self._game_active = False
                self._cancel_turn_timer()
                return

            # =====================================================================
            # 9. BACKEND TURN ROTATION
            # =====================================================================
            forfeited = getattr(self, "_forfeited_players", set())
            active = [i for i in range(self._num_players) if i not in forfeited]

            # Backend turn always preferred
            if turn is not None:
                next_turn = int(turn)
            else:
                next_turn = (actor + 1) % self._num_players

            # If backend turn belongs to forfeited → fix it
            if next_turn not in active:
                next_turn = active[0]

            self._current_player = next_turn
            self._debug(f"[TURN][SYNC] → player {self._current_player}")

            # =====================================================================
            # 10. UNLOCK AND CONTINUE
            # =====================================================================
            self._unlock_and_continue()

        except Exception as e:
            self._debug(f"[SYNC][ERR] {e}")

    # ---------- Turn timer ----------
    def _start_turn_timer(self):
        """Backend-verified 10s idle → auto-roll (online), or offline 10s auto-roll for player 0."""
        self._cancel_turn_timer()

        if not self._game_active:
            self._debug("[TIMER] Game inactive — timer not started.")
            return

        forfeited = getattr(self, "_forfeited_players", set())
        if getattr(self, "_forfeited_players", None) and self._current_player in forfeited:
            active = [i for i in range(self._num_players) if i not in forfeited]
            if not active:
                self._debug("[TIMER] No active players left — stopping game.")
                self._game_active = False
                return
            self._current_player = active[0]
            self._highlight_turn()

        if self._online:
            if self._current_player == self._my_index:
                self._debug(f"[TIMER] 10s auto-roll for player {self._current_player} (you)")

                def _verify_and_roll(dt):
                    try:
                        resp = requests.get(
                            f"{self._backend()}/matches/check",
                            headers={"Authorization": f"Bearer {self._token()}"},
                            params={"match_id": self.match_id},
                            timeout=4,
                            verify=False,
                        )
                        if resp.status_code == 200:
                            srv_turn = int(resp.json().get("turn", -1))
                            if srv_turn == self._my_index:
                                self._debug("[TIMER] Backend confirms your turn → auto-roll")
                                self._auto_roll_real_online()
                            else:
                                self._debug(
                                    f"[TIMER] Skipped auto-roll (srv_turn={srv_turn}, me={self._my_index})"
                                )
                    except Exception as e:
                        self._debug(f"[TIMER][ERR] {e}")

                self._turn_timer = Clock.schedule_once(_verify_and_roll, 10)
            return

        # offline
        if self._current_player == 0:
            self._debug("[TIMER] 10s offline auto-roll for player 0")
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

    def _auto_roll_real_online(self):
        if not self._online or not self._game_active:
            return

        if self._my_index is None:
            self._debug("[AUTO-ROLL] Aborted — player index unknown.")
            self._sync_remote_turn("auto-no-index")
            return

        if getattr(self, "_roll_inflight", False):
            self._debug("[AUTO-ROLL] Aborted — roll already in flight.")
            return

        forfeited = getattr(self, "_forfeited_players", set())
        if self._my_index in forfeited:
            self._debug(f"[AUTO-ROLL] Skip (forfeited player {self._my_index})")
            return

        try:
            resp = requests.get(
                f"{self._backend()}/matches/check",
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"match_id": self.match_id},
                timeout=4,
                verify=False,
            )

            if resp.status_code == 400 or ("Match not active" in resp.text):
                self._debug("[AUTO-ROLL] Backend says match inactive — stopping.")
                self._game_active = False
                self._cancel_turn_timer()
                return

            if resp.status_code != 200:
                self._debug(f"[AUTO-ROLL] check failed {resp.status_code}")
                return

            data = resp.json()
            self._maybe_update_my_index_from_payload(data, trusted=True)
            srv_turn = int(data.get("turn", -1))
            if srv_turn != self._my_index or srv_turn in forfeited:
                self._debug(
                    f"[AUTO-ROLL] skip, srv_turn={srv_turn}, me={self._my_index}, forfeited={forfeited}"
                )
                return
        except Exception as e:
            self._debug(f"[AUTO-ROLL][ERR] {e}")
            return

        self._debug(f"[AUTO-ROLL] confirmed auto-roll for player {self._my_index}")
        self._auto_from_timer = True
        Clock.schedule_once(lambda dt: self.roll_dice(), 0.1)

    def _cancel_turn_timer(self):
        if hasattr(self, "_turn_timer") and self._turn_timer:
            try:
                self._turn_timer.cancel()
            except Exception:
                pass
            self._turn_timer = None

    def _auto_roll_current(self):
        if self._online or not self._game_active:
            return

        if getattr(self, "_bot_rolling", False):
            self._debug("[BOT] Already rolling — skip duplicate auto-roll.")
            return
        self._bot_rolling = True

        current = self._current_player
        delay = random.uniform(1.2, 4.0)
        self._debug(f"[BOT TURN] Player {current} will roll in {delay:.1f}s")

        def do_roll(*_):
            if not self._game_active or self._online:
                self._bot_rolling = False
                return

            roll = random.randint(1, 6)
            self._debug(f"[BOT TURN] Player {current} rolled {roll}")

            try:
                if "dice_button" in self.ids and self.ids.dice_button:
                    self.ids.dice_button.animate_spin(roll)
                else:
                    self._debug("[BOT][WARN] dice_button not ready yet.")
            except Exception as e:
                self._debug(f"[BOT][DICE][ERR] {e}")

            Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)

            def _clear_flag_and_check():
                self._bot_rolling = False
                if self._game_active:
                    self._debug(f"[BOT TURN] Player {current} finished roll.")

            Clock.schedule_once(lambda dt: _clear_flag_and_check(), 1.5)

        Clock.schedule_once(do_roll, delay)

    def _auto_pass_turn(self):
        if not self._online or not self._game_active:
            return

        self._debug(f"[AUTO-TURN] 10s inactivity → passing turn from player {self._current_player}")
        self._current_player = (self._current_player + 1) % self._num_players
        self._highlight_turn()
        self._start_turn_timer()

    def _unlock_and_continue(self):
        try:
            self._mark_roll_end()
            self._end_turn_pending = False
            self._cancel_turn_timer()

            self._debug(f"[TURN] Ready for next roll (player {self._current_player})")
            self._highlight_turn()

            if self._online:
                if self._current_player == self._my_index:
                    self._debug("[TIMER] 10s auto-roll timer (you)")
                    self._turn_timer = Clock.schedule_once(
                        lambda dt: self._auto_roll_real_online(), 10
                    )
                return

            if self._current_player == 0:
                self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

        except Exception as e:
            self._debug(f"[UNLOCK][ERR] {e}")

    # ---------- player info ----------
    def show_player_info(self, idx: int):
        pids = storage.get_player_ids() if storage else [None, None, None]
        names = [self.player1_name, self.player2_name, self.player3_name]
        pid = pids[idx] if idx < len(pids) else None
        name = names[idx] if idx < len(names) else f"Player {idx+1}"
        self._open_player_popup(name, 0, self._resolve_avatar_source(idx, name, pid))

    def _open_player_popup(self, name: str, balance: int, image_source: str):
        layout = BoxLayout(orientation="vertical", spacing=10, padding=12)
        layout.add_widget(Image(source=image_source, size_hint=(1, 0.65)))
        layout.add_widget(Label(text=name, halign="center", size_hint=(1, 0.175)))
        layout.add_widget(Label(text=f"Wallet: ₹{balance}", halign="center", size_hint=(1, 0.175)))
        Popup(title="Player Info", content=layout, size_hint=(None, None), size=(300, 400)).open()

    # ---------- deprecated animator ----------
    def _animate_diff(self, old_positions, new_positions, reverse=False, actor=None, roll=None, spawn=False):
        self._debug("[ANIM] Deprecated _animate_diff() called — using unified _on_server_event flow.")
