import os
import json
import random
import requests
import threading

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
            keep_ratio=True
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
        """Animate dice: two clockwise rotations + one counterclockwise, then set the final face."""
        self.stop_spin()

        # --- Spin animations: 2 clockwise, 1 counterclockwise ---
        spin_seq = (
                Animation(_rotation_angle=360, d=0.25, t="linear") +  # 1st clockwise
                Animation(_rotation_angle=720, d=0.25, t="linear") +  # 2nd clockwise
                Animation(_rotation_angle=540, d=0.25, t="linear")  # counterclockwise (720 → 540)
        )

        # --- Subtle zoom effect for realism ---
        zoom = (
                Animation(scale_value=1.2, d=0.15, t="out_quad") +
                Animation(scale_value=1.0, d=0.2, t="out_quad")
        )

        # --- Sync rotation with visual transform ---
        spin_seq.bind(on_progress=lambda *_: self._sync_rotation())

        # --- Run both animations ---
        zoom.start(self)
        spin_seq.start(self)
        self._anim = spin_seq

        # --- Set final dice face once animation completes ---
        def set_final_face(*_):
            img_path = os.path.join("assets", "dice", f"dice{result}.png")
            if os.path.exists(img_path):
                self._dice_image.source = img_path
                self._dice_image.reload()

            # Reset transforms
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
        self._my_index = 0
        self._roll_inflight = False
        self._last_roll_seen = None
        # signature used to suppress true duplicates, but still allow reverse/spawn/turn updates
        self._last_state_sig = None  # (positions tuple, last_roll, reverse, spawn, turn)
        self._last_roll_animated = None

    # ---------- helpers ----------
    def _root_float(self):
        """Always use the dedicated overlay from KV."""
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
                parent.remove_widget(w)
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

        # Detect if it's actually a bot/offline game
        player2 = self.player2_name.lower().strip() if self.player2_name else ""
        bot_names = ["bot", "crazy boy", "kurfi", "sharp"]

        if player2 in bot_names or (not mid):
            # ✅ Force offline mode for bot play
            self._online = False
            self.match_id = None
            if storage:
                try:
                    storage.set_current_match(None)  # clear any stale match
                except Exception:
                    pass
            self._debug("[MODE] Bot/Offline mode forced (no server sync)")
            self._highlight_turn()
        else:
            # ✅ True online match
            self._online = True
            self.match_id = mid
            self._debug(f"[MODE] Online match detected (ID={mid})")
            self._start_online_sync()

    def on_leave(self, *_):
        self._stop_online_sync()

    # ---------- portraits ----------
    def _resolve_avatar_source(self, index: int, name: str, pid):
        bot_map_by_id = {-1000: "assets/bot_sharp.png", -1001: "assets/bot_crazy.png", -1002: "assets/bot_kurfi.png"}
        bot_map_by_name = {"sharp": "assets/bot_sharp.png", "crazy boy": "assets/bot_crazy.png", "kurfi": "assets/bot_kurfi.png"}
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
        self._positions = [0, 0, 0]
        self._spawned_on_board = [False, False, False]
        self.dice_result = ""
        self._winner_shown = False
        self._game_active = True
        self._current_player = random.randint(0, self._num_players - 1)
        self._place_coins_near_portraits()
        if not self._online:
            self._highlight_turn()

    # ---------- external control ----------
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
        # Ensure bot games never connect to server
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
        try:
            uid = (storage.get_user() or {}).get("id") if storage else None
            pids = storage.get_player_ids() if storage else []
            num = storage.get_num_players() or self._num_players
            for i, pid in enumerate((pids or [])[:num]):
                if pid is not None and pid == uid:
                    self._my_index = i
                    break
            else:
                self._my_index = 0
        except Exception as e:
            print(f"[INDEX][WARN] resolve failed: {e}")
            self._my_index = 0

    # ---------- turn ----------
    def _highlight_turn(self):
        """Highlight active player and handle bot or timer logic safely."""
        p1_overlay = self.ids.get("p1_overlay")
        p2_overlay = self.ids.get("p2_overlay")
        p3_overlay = self.ids.get("p3_overlay")

        def pulse(widget, active: bool):
            if not widget:
                return
            Animation.cancel_all(widget, 'opacity')
            if active:
                anim = (Animation(opacity=0.6, d=1.0, t="in_out_quad") +
                        Animation(opacity=0.2, d=1.0, t="in_out_quad"))
                anim.repeat = True
                anim.start(widget)
            else:
                widget.opacity = 0

        # Highlight the correct player's overlay
        pulse(p1_overlay, self._current_player == 0)
        pulse(p2_overlay, self._current_player == 1)
        pulse(p3_overlay, self._current_player == 2 and bool(self.player3_name))

        # --- Behavior depending on mode ---
        if self._online:
            # ✅ Do not start multiple timers here — only manage visuals
            self._debug(f"[TURN][UI] Online turn highlight for player {self._current_player}")
            return

        # --- OFFLINE MODE ---
        if self._current_player != 0:
            self._debug(f"[BOT TURN] Player {self._current_player} auto-roll soon")
            Clock.schedule_once(lambda dt: self._auto_roll_current(), 0.3)
        else:
            # Real offline player — start auto-roll timer
            self._debug("[TIMER] Real player idle → auto-roll in 10s (offline)")
            self._cancel_turn_timer()
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

    # ---------- dice ----------
    def roll_dice(self):
        """Roll dice for both online (server) and offline (bot) modes."""
        if not self._game_active:
            return

        # --- OFFLINE / BOT MODE ---
        if not self._online:
            # Prevent multiple rolls per turn (only for offline human)
            if getattr(self, "_roll_locked", False):
                self._debug("[ROLL] Turn already rolled — ignoring extra clicks.")
                return

            if self._current_player != 0:
                self._debug("[TURN] Not your turn (bot playing).")
                self._show_temp_popup("Not your turn!", duration=1.5)
                return

            # Lock only when we are about to roll
            self._roll_locked = True

            roll = random.randint(1, 6)
            self._debug(f"[BOT/OFFLINE] Player {self._current_player} rolled {roll}")

            if "dice_button" in self.ids:
                try:
                    self.ids.dice_button.animate_spin(roll)
                except Exception as e:
                    self._debug(f"[BOT][DICE][ERR] {e}")

            Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)
            return

        # --- ONLINE MODE ---
        if not self.match_id or not self._token():
            self._debug("[ROLL] Missing match_id or token — aborting online roll.")
            return

        # Must be our turn to ask server to roll
        if self._current_player != self._my_index:
            self._debug("[TURN] Not your turn!")
            # Only show popup for manual clicks, not auto-roll
            if not getattr(self, "_auto_from_timer", False):
                self._show_temp_popup("Not your turn!", duration=1.5)
            return

        # Prevent duplicate in-flight request
        if getattr(self, "_roll_inflight", False):
            self._debug("[ROLL] Roll already in progress — ignored")
            return

        # Lock only now, after turn validation
        self._roll_locked = True
        self._roll_inflight = True

        def _resync_and_timer():
            """Single-shot resync after 409 and restart timer."""
            try:
                resp2 = requests.get(
                    f"{self._backend()}/matches/check",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    params={"match_id": self.match_id},
                    timeout=6,
                    verify=False,
                )
                if resp2.status_code == 200:
                    Clock.schedule_once(lambda dt: self._on_server_event(resp2.json()), 0)
            except Exception as e:
                self._debug(f"[ROLL][RESYNC][ERR] {e}")
            finally:
                Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.05)

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
                    roll = int(data.get("roll") or 1)
                    Clock.schedule_once(lambda dt: self._animate_dice_and_apply_server(data, roll), 0)
                elif resp.status_code == 409:
                    self._debug("[TURN] Server rejected roll — not your turn")
                    # Only show popup if it was a manual press
                    if not getattr(self, "_auto_from_timer", False):
                        Clock.schedule_once(lambda dt: self._show_temp_popup("Not your turn!", duration=1.5), 0)
                    # Clear locks early and resync once
                    Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 0)
                    Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 0)
                    Clock.schedule_once(lambda dt: _resync_and_timer(), 0.05)
                else:
                    # Handle stuck state gracefully
                    if "Match not active" in resp.text:
                        Clock.schedule_once(lambda dt: self._safe_turn_recover(), 0.2)
                    self._debug(f"[ROLL][HTTP] Ignored {resp.status_code}: {resp.text}")
            except Exception as e:
                self._debug(f"[ROLL][ERR] {e}")
            finally:
                # If not already cleared (e.g., 409 path above), clear now
                Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 0.2)
                Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 0.2)
                # Clear auto flag if it was set
                if getattr(self, "_auto_from_timer", False):
                    Clock.schedule_once(lambda dt: setattr(self, "_auto_from_timer", False), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _safe_turn_recover(self):
        """Recover if match stuck in 'not your turn' after spawn."""
        if not self._game_active:
            return
        self._debug("[TURN][RECOVER] Advancing turn manually after stuck spawn.")
        self._roll_locked = False
        self._roll_inflight = False
        self._current_player = (self._current_player + 1) % self._num_players
        self._highlight_turn()

    def _offline_roll_action(self):
        """Perform offline dice roll with animation and movement."""
        if getattr(self, "_roll_locked", False):
            self._debug("[ROLL] Turn already rolled — ignoring duplicate offline roll.")
            return
        self._roll_locked = True

        roll = random.randint(1, 6)
        self._debug(f"[OFFLINE MODE] Player {self._current_player} rolled {roll}")

        if "dice_button" in self.ids:
            try:
                self.ids.dice_button.animate_spin(roll)
            except Exception as e:
                self._debug(f"[DICE][ERR] {e}")

        # Apply roll after animation delay (0.8s)
        Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)

        # Reset roll flag safely (for next turn)
        Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 1.0)
        Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 1.0)

    # ---------- helper popup ----------
    def _show_temp_popup(self, msg: str, duration: float = 1.5):
        """Show a quick auto-closing popup message."""
        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text=msg, halign="center"))
        popup = Popup(title="Notice", content=layout,
                      size_hint=(None, None), size=(250, 150), auto_dismiss=True)
        popup.open()
        Clock.schedule_once(lambda dt: popup.dismiss(), duration)

    def _animate_dice_and_apply_server(self, data, roll):
        """Spin dice face, then apply payload from server."""
        if "dice_button" in self.ids:
            self.ids.dice_button.animate_spin(roll)
        # Let unified handler move coins & update state
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

        # stop any in-flight anims to prevent drift
        Animation.cancel_all(coin)

        # coin size based on the row height
        h = getattr(box, "height", dp(50))
        size_px = max(dp(40), min(dp(64), h * 0.9))
        coin.size = (size_px, size_px)

        layer = self._root_float()

        # simple, stable stacking by player index (no overlap)
        # 0 → left, 1 → center, 2 → right
        stack_x = {-1: -dp(14), 0: 0, 1: dp(14)}.get(idx - 1, 0)
        stack_y = 0

        if reverse:
            # snap/animate to start tile
            start_anchor = self.ids.get("box_0")
            if start_anchor:
                tx, ty = self._map_center_to_parent(layer, start_anchor)
                Animation(center=(tx, ty), d=0.5, t="out_quad").start(coin)
                self._positions[idx] = 0
            else:
                self._debug("[WARN] box_0 missing; reverse skipped")
            return

        # normal placement: map target tile center → overlay space and add offset
        tx, ty = self._map_center_to_parent(layer, box)
        Animation(center=(tx + stack_x, ty + stack_y), d=0.5, t="out_quad").start(coin)
        self._positions[idx] = pos

        self._debug(f"[MOVE] Player {idx} now at {pos}")

    def _apply_positions_to_board(self, positions, reverse=False):
        """Place all coins to the given positions without special logic."""
        self._ensure_coin_widgets()
        self._num_players = 3 if (len(positions) >= 3 and self.player3_name) else 2
        for idx in range(self._num_players):
            self._positions[idx] = int(positions[idx])
            self._move_coin_to_box(idx, int(positions[idx]), reverse=False)

    # ---------- roll logic (OFFLINE) ----------
    def _apply_roll(self, roll: int):
        """
        Offline dice roll logic — perfectly mirrors backend.
        Handles spawn (1), reverse at 3, overshoot (>7 stays), and win (==7).
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
                self._debug(f"[SPAWN] Player {p} enters board at box 0")
            else:
                self._debug(f"[SKIP] Player {p} not spawned (roll={roll})")
            Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.5)
            return

        # --- Rule 2: Reverse (danger box 3) ---
        if new_pos == DANGER_BOX:
            self._debug(f"[DANGER] Player {p} hit box {DANGER_BOX} → reset to start")
            self._positions[p] = 0
            self._move_coin_to_box(p, DANGER_BOX)

            # ✅ Important: prevent false win during animation
            self._game_active = False  # temporarily pause game flow

            def do_reverse_reset(*_):
                self._move_coin_to_box(p, 0, reverse=True)
                self._debug(f"[RESET] Player {p} safely returned to start")
                self._game_active = True
                self._end_turn_and_highlight()

            Clock.schedule_once(do_reverse_reset, 0.8)
            return

        # --- Rule 3: Exact win (==7) ---
        if new_pos == BOARD_MAX:
            self._positions[p] = BOARD_MAX
            self._move_coin_to_box(p, BOARD_MAX)
            self._declare_winner(p)
            return

        # --- Rule 4: Overshoot (>7) → stay ---
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
        Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.6)

    def _end_turn_and_highlight(self):
        """Safely advance to the next turn and trigger proper highlight/bot roll."""
        if getattr(self, "_end_turn_pending", False):
            self._debug("[TURN] End-turn already pending — skipping duplicate call.")
            return
        self._end_turn_pending = True

        self._roll_locked = False
        self._current_player = (self._current_player + 1) % self._num_players
        self._debug(f"[TURN] Switching to player {self._current_player}")

        def finish_turn(*_):
            self._end_turn_pending = False
            self._highlight_turn()
            # Always start a new timer for whoever’s next
            self._start_turn_timer()

        Clock.schedule_once(finish_turn, 0.3)

    # ---------- victory ----------
    def _declare_winner(self, winner_idx: int):
        """Show victory popup and auto-close to stage screen."""
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

        # ✅ Auto-close popup and return to stage screen
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
        """Handle Give Up — notify backend + close both clients cleanly."""
        self._game_active = False
        backend, token, match_id = self._backend(), self._token(), self.match_id
        if not (backend and token and match_id):
            self._show_forfeit_popup("You gave up! Opponent wins.")
            return

        my_index = self._my_index  # Track who gave up

        def worker():
            try:
                resp = requests.post(
                    f"{backend}/matches/forfeit",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"match_id": match_id, "actor": my_index},
                    timeout=10,
                    verify=False,
                )
                data = resp.json() if resp.status_code == 200 else {}
                winner = data.get("winner_name", "Opponent")

                # --- Notify other clients (WebSocket broadcast) ---
                try:
                    notify = {
                        "forfeit": True,
                        "match_id": match_id,
                        "actor": my_index,  # 👈 tell who gave up
                        "winner": data.get("winner"),
                        "winner_name": winner,
                    }
                    requests.post(
                        f"{backend}/matches/check",
                        headers={"Authorization": f"Bearer {token}"},
                        json=notify,
                        timeout=5,
                        verify=False,
                    )
                except Exception:
                    pass

                # --- Local popup for the quitter ---
                Clock.schedule_once(lambda dt: self._show_forfeit_popup("You gave up! Opponent wins."), 0)
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 3.0)

            except Exception as e:
                self._debug(f"[FORFEIT][ERR] {e}")
                Clock.schedule_once(lambda dt: self._show_forfeit_popup("You gave up! Opponent wins."), 0)
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 3.0)

        threading.Thread(target=worker, daemon=True).start()

    def _show_forfeit_popup(self, msg: str):
        """Show defeat popup (auto-close in 2.5s)."""
        layout = BoxLayout(orientation="vertical", spacing=10, padding=10)
        layout.add_widget(Label(text=msg, halign="center"))

        popup = Popup(
            title="Defeat",
            content=layout,
            size_hint=(None, None),
            size=(300, 200),
            auto_dismiss=True,
        )
        popup.open()

        # ✅ Auto-close popup and go back to stage
        def close_popup_and_reset(*_):
            try:
                popup.dismiss()
            except Exception:
                pass
            self._reset_after_popup()

        Clock.schedule_once(close_popup_and_reset, 2.5)

    # ---------- Turn timer (Auto-Pass & Auto-Roll) ----------
    def _start_turn_timer(self):
        """
        Start a timer for each turn.
        - ONLINE: start only if backend confirmed it's *your* turn.
        - OFFLINE: real player auto-rolls in 10 s if idle.
        """
        self._cancel_turn_timer()
        if not self._game_active:
            return

        current = self._current_player

        # --- ONLINE MODE ---
        if self._online:
            # only schedule if backend turn matches your index
            if current == self._my_index:
                self._debug(f"[TIMER] Started 10s auto-roll timer for player {current} (you)")
                # wait a small buffer (1 s) to let backend propagate state before arming timer
                self._turn_timer = Clock.schedule_once(lambda dt: self._auto_roll_real_online(), 10)
            else:
                self._debug(f"[TIMER] Not your turn (server turn={current}, me={self._my_index}) — skip auto-roll")
            return

        # --- OFFLINE MODE ---
        if current == 0:
            self._debug("[TIMER] Real player idle → auto-roll in 10 s")
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)
        else:
            self._debug(f"[BOT TIMER] Player {current} will auto-roll soon (via _auto_roll_current)")
            self._turn_timer = Clock.schedule_once(lambda dt: self._auto_roll_current(), 0)

    def _auto_roll_real_online(self):
        """
        Auto-roll for the real player in ONLINE mode after 10s of inactivity.
        """
        if not self._online or not self._game_active:
            return

        # Only if it's still our turn
        if self._current_player != self._my_index:
            self._debug("[AUTO-ROLL] Skipped — turn already passed to opponent.")
            return

        # Mark origin to suppress popups on 409 and to avoid early lock issues
        self._auto_from_timer = True

        # Make sure no stale locks block us
        self._roll_locked = False
        self._roll_inflight = False

        self._debug(f"[AUTO-ROLL] 10s inactivity → auto rolling for player {self._my_index}")

        # (Optional) quick pre-spin so users see feedback even if server is slow
        try:
            if "dice_button" in self.ids and self.ids.dice_button:
                dummy_roll = random.randint(1, 6)
                self.ids.dice_button.animate_spin(dummy_roll)
        except Exception as e:
            self._debug(f"[AUTO-ROLL][DICE][ERR] {e}")

        # Use the same path as manual roll; worker will clear _auto_from_timer
        self.roll_dice()

    def _cancel_turn_timer(self):
        """Cancel any active turn timer."""
        if hasattr(self, "_turn_timer") and self._turn_timer:
            try:
                self._turn_timer.cancel()
            except Exception:
                pass
            self._turn_timer = None

    def _auto_roll_current(self):
        """Auto-roll for bot/offline players with natural delay and animation."""
        if self._online or not self._game_active:
            return

        # ✅ Prevent overlapping or duplicate bot rolls
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

            # ---- Animate dice ----
            try:
                if "dice_button" in self.ids and self.ids.dice_button:
                    self.ids.dice_button.animate_spin(roll)
                else:
                    self._debug("[BOT][WARN] dice_button not ready yet.")
            except Exception as e:
                self._debug(f"[BOT][DICE][ERR] {e}")

            # ---- Apply roll after spin delay (so movement matches dice animation) ----
            Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)

            # ---- Safely clear bot flag after roll completes ----
            def _clear_flag_and_check():
                self._bot_rolling = False
                if self._game_active:
                    self._debug(f"[BOT TURN] Player {current} finished roll.")

            Clock.schedule_once(lambda dt: _clear_flag_and_check(), 1.5)

        # Schedule the roll after a natural reaction delay
        Clock.schedule_once(do_roll, delay)

    def _auto_pass_turn(self):
        """Auto-pass turn to next player after 10 seconds (ONLINE mode)."""
        if not self._online or not self._game_active:
            return

        self._debug(f"[AUTO-TURN] 10s inactivity → passing turn from player {self._current_player}")
        self._current_player = (self._current_player + 1) % self._num_players
        self._highlight_turn()
        self._start_turn_timer()

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

    # ---------- ONLINE sync ----------
    def _start_online_sync(self):
        self._stop_online_sync()
        self._resolve_my_index()
        self._last_roll_seen = None
        self._last_state_sig = None
        self._last_roll_animated = None
        if WEBSOCKET_OK:
            self._ws_stop.clear()
            self._ws_thread = threading.Thread(target=self._ws_worker, daemon=True)
            self._ws_thread.start()
        else:
            self._poll_ev = Clock.schedule_interval(lambda dt: self._poll_state_once(), 0.9)

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

    # ---------- core server event handler ----------
    def _on_server_event(self, payload: dict):
        """Handle real-time updates from backend (positions, roll, turn, winner, forfeit)."""
        try:
            positions = payload.get("positions") or self._positions
            roll = payload.get("last_roll")
            actor = payload.get("actor")
            reverse = bool(payload.get("reverse", False))
            spawn = bool(payload.get("spawn", False))
            winner = payload.get("winner")
            turn = payload.get("turn")
            finished = payload.get("finished", False)
            forfeit_flag = payload.get("forfeit", False)

            # --- Deduplicate identical payloads ---
            sig = (tuple(positions), int(roll) if roll else None, int(turn) if turn is not None else None)
            if getattr(self, "_last_state_sig", None) == sig:
                self._debug("[SYNC] Duplicate state – ignored")
                return
            self._last_state_sig = sig

            # --- Animate dice if roll provided ---
            if roll and "dice_button" in self.ids:
                self._debug(f"[ROLL] Actor={actor} rolled={roll}")
                try:
                    self.ids.dice_button.animate_spin(int(roll))
                except Exception as e:
                    self._debug(f"[ROLL][ANIM][ERR] {e}")

            # --- Detect which player moved if actor not in payload ---
            if actor is None:
                diffs = [i for i, (a, b) in enumerate(zip(self._positions, positions)) if a != b]
                actor = diffs[0] if diffs else 0

            old_positions = self._positions[:]
            self._positions = [int(p) for p in positions]
            self._ensure_coin_widgets()

            # --- Spawn handling ---
            if spawn and actor is not None:
                self._debug(f"[SPAWN] Player {actor} enters board at 0")
                self._move_coin_to_box(actor, 0)
                self._spawned_on_board[actor] = True
                self._current_player = int(turn or (actor + 1) % self._num_players)
                self._unlock_and_continue()
                return

            # --- Reverse move ---
            if reverse and actor is not None:
                self._debug(f"[REVERSE] Player {actor} returning to 0")
                Clock.schedule_once(lambda dt: self._move_coin_to_box(actor, 0, reverse=True), 0.6)
                self._unlock_and_continue()
                return

            # --- Normal move update ---
            moved = False
            for idx, (old, new) in enumerate(zip(old_positions, positions)):
                if old != new:
                    moved = True
                    self._debug(f"[MOVE] Player {idx} {old}→{new}")
                    self._move_coin_to_box(idx, new)
                else:
                    self._debug(f"[SYNC] Player {idx} stays at {new}")

            if not moved and roll and actor is not None:
                self._debug(f"[OVERSHOOT] Player {actor} no valid move (roll={roll})")

            # --- Handle victory ---
            if winner is not None:
                Clock.schedule_once(lambda dt: self._declare_winner(int(winner)), 0.8)
                return

            # --- Handle forfeit / finish properly ---
            if forfeit_flag or finished:
                # show correct popup depending on who forfeited
                if winner == self._my_index:
                    msg = "Opponent gave up! You win!"
                elif winner is not None and winner != self._my_index:
                    msg = "You gave up! Opponent wins."
                else:
                    msg = "Match ended."
                self._debug(f"[FORFEIT] {msg}")
                self._show_forfeit_popup(msg)
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)
                return

            # --- Turn update (fallback-safe) ---
            prev_turn = self._current_player
            if turn is not None:
                try:
                    self._current_player = int(turn)
                    self._debug(f"[TURN][SYNC] Updated from server: {self._current_player}")
                except Exception as e:
                    self._debug(f"[TURN][SYNC][ERR] {e}")
            else:
                next_turn = (actor + 1) % self._num_players
                self._debug(f"[TURN][FALLBACK] Missing turn info — switched to {next_turn}")
                self._current_player = next_turn

            # --- Only unlock once per new turn ---
            if prev_turn != self._current_player:
                self._unlock_and_continue()
            else:
                self._debug("[TURN][SYNC] Same player retained — skipping unlock to avoid duplicate triggers")

        except Exception as e:
            self._debug(f"[SYNC][ERR] {e}")

    def _unlock_and_continue(self):
        """Safely unlock roll flags, refresh UI, and schedule next turn timer once."""
        try:
            # --- Reset roll/turn flags safely ---
            self._roll_locked = False
            self._roll_inflight = False
            self._end_turn_pending = False

            # --- Clear any leftover timer to avoid duplicates ---
            self._cancel_turn_timer()

            # --- Debug log for clarity ---
            self._debug(f"[TURN] Ready for next roll (player {self._current_player})")

            # --- Update visuals ---
            self._highlight_turn()

            # --- ONLINE mode: start timer only for current local player ---
            if self._online:
                if self._current_player == self._my_index:
                    self._debug(f"[TIMER] Started 10s auto-roll timer for player {self._current_player} (you)")
                    self._turn_timer = Clock.schedule_once(lambda dt: self._auto_roll_real_online(), 10)
                else:
                    self._debug(
                        f"[TIMER] Not your turn (server turn={self._current_player}, me={self._my_index}) — skip auto-roll")
            else:
                # --- OFFLINE: handled by _highlight_turn itself (bot/real) ---
                pass

        except Exception as e:
            self._debug(f"[UNLOCK][ERR] {e}")

    # ---------- (deprecated) fallback animator ----------
    def _animate_diff(self, old_positions, new_positions, reverse=False, actor=None, roll=None, spawn=False):
        """Deprecated: kept for compatibility if somebody calls it."""
        try:
            self._debug("[ANIM] Deprecated _animate_diff() called — using unified _on_server_event flow.")
        except Exception:
            pass
