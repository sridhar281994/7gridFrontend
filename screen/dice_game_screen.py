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
        """
        Reset or resume game state safely after start or after a player forfeit.
        Adds 1s sync delay before starting the first auto-roll timer.
        """
        self._positions = [0, 0, 0]
        self._spawned_on_board = [False, False, False]
        self.dice_result = ""
        self._winner_shown = False
        self._game_active = True

        # Handle forfeited players if any exist
        forfeited = getattr(self, "_forfeited_players", set())
        active_players = [i for i in range(self._num_players) if i not in forfeited]
        if not active_players:
            self._debug("[RESET] All players forfeited — stopping game.")
            self._game_active = False
            return

        # Pick current player from active ones
        self._current_player = random.choice(active_players)
        self._place_coins_near_portraits()

        # Highlight the starting or continuing player
        self._highlight_turn()

        # --- AUTO-ROLL HANDLER AT GAME START (with 1s backend alignment delay) ---
        def auto_roll_if_idle(*_):
            if not self._game_active:
                return
            if getattr(self, "_roll_inflight", False) or getattr(self, "_roll_locked", False):
                return  # already rolled manually

            # Skip forfeited players
            if self._current_player in forfeited:
                self._debug(f"[AUTO-ROLL-INIT] Skipping forfeited player {self._current_player}")
                self._current_player = next(
                    (p for p in active_players if p != self._current_player),
                    active_players[0],
                )
                self._highlight_turn()

            # Trigger auto-roll depending on mode
            if not self._online:
                if self._current_player == 0:
                    self._debug("[AUTO-ROLL-INIT] 10s idle → offline auto-roll for player 0.")
                    self.roll_dice()
            else:
                if self._current_player == getattr(self, "_my_index", 0):
                    self._debug("[AUTO-ROLL-INIT] 10s idle → online auto-roll for real player.")
                    self.roll_dice()

        # ✅ Wait 1s for backend sync, then start the 10s idle timer
        def delayed_start(*_):
            self._debug("[AUTO-ROLL-INIT] Startup delay (1s) before enabling 10s idle check.")
            Clock.schedule_once(auto_roll_if_idle, 10)

        Clock.schedule_once(delayed_start, 1)

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
    # ---------- dice ----------
    def roll_dice(self):
        """Roll dice for both online (server) and offline (bot) modes."""
        if not self._game_active:
            return

        now = time.time()
        if hasattr(self, "_last_roll_time") and now - getattr(self, "_last_roll_time", 0) < 1.5:
            self._debug("[ROLL] Ignoring duplicate roll trigger within 1.5s window.")
            return
        self._last_roll_time = now

        # ---------- OFFLINE ----------
        if not self._online:
            if getattr(self, "_roll_inflight", False):
                return
            self._roll_locked = False
            if self._current_player != 0:
                self._show_temp_popup("Not your turn!", duration=1.8)
                return
            self._roll_locked = True
            self._roll_inflight = True
            roll = random.randint(1, 6)
            if "dice_button" in self.ids:
                self.ids.dice_button.animate_spin(roll)
            Clock.schedule_once(lambda dt: self._apply_roll(roll), 0.8)
            Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 1.0)
            Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 1.0)
            return

        # ---------- ONLINE ----------
        if not self.match_id or not self._token():
            self._debug("[ROLL] Missing match_id or token — aborting online roll.")
            return

        # One-time sync of first turn
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
                    srv_turn = resp.json().get("turn")
                    if srv_turn is not None:
                        self._current_player = int(srv_turn)
            except Exception as e:
                self._debug(f"[TURN][SYNC][ERR] {e}")

        # --- Block manual clicks when not your turn (except auto-roll) ---
        if self._current_player != self._my_index:
            if not getattr(self, "_auto_from_timer", False):
                self._show_temp_popup("Not your turn!", duration=1.8)
            return

        if getattr(self, "_roll_inflight", False):
            self._debug("[ROLL] Ignored — already in flight.")
            return

        self._roll_locked = False
        self._roll_inflight = True

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
                    self._debug("[TURN] Server rejected roll — not your turn.")
                    if not getattr(self, "_auto_from_timer", False):
                        self._show_temp_popup("Not your turn!", duration=1.5)
                    Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 0)
                    Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 0)
                    Clock.schedule_once(lambda dt: setattr(self, "_last_roll_time", 0), 0)
                    return
                elif resp.status_code == 400 and "Match not active" in resp.text:
                    self._debug("[ROLL] Match not active — triggering safe recovery.")
                    Clock.schedule_once(lambda dt: self._safe_turn_recover(), 0.2)
                else:
                    self._debug(f"[ROLL][HTTP] Unexpected {resp.status_code}: {resp.text}")
            except Exception as e:
                self._debug(f"[ROLL][ERR] {e}")
            finally:
                # always clear cooldown flags even on errors
                Clock.schedule_once(lambda dt: setattr(self, "_roll_inflight", False), 0.1)
                Clock.schedule_once(lambda dt: setattr(self, "_roll_locked", False), 0.1)
                Clock.schedule_once(lambda dt: setattr(self, "_last_roll_time", 0), 0.1)
                if getattr(self, "_auto_from_timer", False):
                    Clock.schedule_once(lambda dt: setattr(self, "_auto_from_timer", False), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _safe_turn_recover(self):
        """Recover from a stuck 'not your turn' state without killing auto-roll."""
        if not self._game_active:
            return

        # Always clear any stale timers/locks first
        self._cancel_turn_timer()
        self._roll_locked = False
        self._roll_inflight = False
        self._end_turn_pending = False

        # During very first-turn sync, don't advance locally; just realign and (re)start timer.
        if not getattr(self, "_first_turn_synced", False):
            self._debug("[TURN][RECOVER] In first-turn sync window — no local advance; restarting timer.")
            self._highlight_turn()
            # small delay so UI is ready, then start timer (it will auto-roll only if it's your turn)
            Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.25)
            return

        # Try to realign with backend once before any local advance
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
                backend_turn = int(data.get("turn", self._current_player))
                if backend_turn != self._current_player:
                    self._debug(f"[TURN][RECOVER] Resynced turn {self._current_player} → {backend_turn}")
                    self._current_player = backend_turn
                    self._highlight_turn()
                    # restart timer; in ONLINE mode it will only arm if it's your turn
                    Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.25)
                    return
                else:
                    # backend agrees with our turn; just restart timer
                    self._debug("[TURN][RECOVER] Backend agrees — restarting timer.")
                    self._highlight_turn()
                    Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.25)
                    return
            else:
                self._debug(f"[TURN][RECOVER] Check failed {resp.status_code} — fallback advance.")
        except Exception as e:
            self._debug(f"[TURN][RECOVER][ERR] {e} — fallback advance.")

        # Last resort: locally advance one seat to unstick, then restart timer
        self._current_player = (self._current_player + 1) % self._num_players
        self._debug(f"[TURN][RECOVER] Local fallback advance → player {self._current_player}")
        self._highlight_turn()
        Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.25)

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
    def _show_temp_popup(self, msg: str, duration: float = 2.0):
        """Toast popup that stays visible for the requested duration."""
        try:
            if not hasattr(self, "_toast_popup") or self._toast_popup is None:
                layout = BoxLayout(orientation="vertical", spacing=8, padding=(14, 12, 14, 12))
                self._toast_label = Label(text=msg, halign="center", valign="middle")
                self._toast_label.bind(size=lambda *_: setattr(self._toast_label, "text_size", self._toast_label.size))
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

            # Minimum visible time fixed to 1.5 s
            self._toast_ev = Clock.schedule_once(_close, max(1.5, float(duration)))

        except Exception as e:
            self._debug(f"[TOAST][ERR] {e}")

    def _animate_dice_and_apply_server(self, data, roll):
        """Spin dice face, then apply payload from server."""
        if "dice_button" in self.ids:
            self.ids.dice_button.animate_spin(roll)
        # Let unified handler move coins & update state
        Clock.schedule_once(lambda dt: self._on_server_event(data), 0.8)

    # ---------- coins ----------
    def _ensure_coin_widgets(self):
        """Ensure coin image widgets exist and are properly attached to the overlay."""
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
        """Place coins initially near player portraits (before entering the board)."""
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
        """Move the specified coin to the target box (with smooth animation)."""
        box = self.ids.get(f"box_{pos}")
        coin = self._coins[idx] if idx < len(self._coins) else None
        if not box or not coin:
            return

        # Stop any running animations to avoid drift
        Animation.cancel_all(coin)

        # Coin size scaled to row height for responsiveness
        h = getattr(box, "height", dp(50))
        size_px = max(dp(40), min(dp(64), h * 0.9))
        coin.size = (size_px, size_px)

        layer = self._root_float()

        # Deterministic stacking offset by player index
        # 0 → left, 1 → center, 2 → right
        offsets = {0: -dp(14), 1: 0, 2: dp(14)}
        stack_x = offsets.get(idx, 0)
        stack_y = 0

        # Reverse animation → move back to box_0
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

        # Normal move animation
        tx, ty = self._map_center_to_parent(layer, box)
        Animation(center=(tx + stack_x, ty + stack_y), d=0.5, t="out_quad").start(coin)
        self._positions[idx] = pos
        self._debug(f"[MOVE] Player {idx} now at {pos}")

    def _apply_positions_to_board(self, positions, reverse=False):
        """Apply given board state to all coins (used on sync/refresh)."""
        self._ensure_coin_widgets()
        self._num_players = 3 if (len(positions) >= 3 and self.player3_name) else 2
        for idx in range(self._num_players):
            self._positions[idx] = int(positions[idx])
            self._move_coin_to_box(idx, int(positions[idx]), reverse=False)

    # ---------- roll logic (OFFLINE) ----------
    def _apply_roll(self, roll: int):
        """
        Offline dice roll logic — mirrors backend.
        Rules:
          - Roll 1: spawn coin into box 0.
          - Land on box 3: reset to start.
          - >7: stay.
          - ==7: win.
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

        # --- Rule 1: Spawn (1) ---
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

        # --- Rule 2: Danger zone (box 3 → reset) ---
        if new_pos == DANGER_BOX:
            self._debug(f"[DANGER] Player {p} hit box 3 → reset to start")
            self._positions[p] = 0
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

        # --- Rule 4: Overshoot (>7) ---
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
        """Safely advance to next player's turn."""
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
        """Handle Give Up — cleanly notify backend and prevent duplicate calls."""
        if getattr(self, "_forfeit_lock", False):
            self._debug("[FORFEIT] Click ignored (lock active).")
            return
        self._forfeit_lock = True
        Clock.schedule_once(lambda dt: setattr(self, "_forfeit_lock", False), 3.0)

        self._debug("[FORFEIT] Give Up pressed.")
        backend, token, match_id = self._backend(), self._token(), self.match_id
        self._game_active = False

        # Offline fallback (no match id)
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

                # Handle already finished gracefully
                if resp.status_code == 400 and "already finished" in resp.text.lower():
                    self._debug("[FORFEIT] Match already finished — skipping.")
                    Clock.schedule_once(lambda dt: self._reset_after_popup(), 1.5)
                    return

                # Partial forfeit (3-player game continues)
                if data.get("continuing"):
                    Clock.schedule_once(lambda dt:
                                        self._show_forfeit_popup("You gave up! Others continue playing."), 0)
                    Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)
                    return

                winner = data.get("winner_name", "Opponent")
                Clock.schedule_once(lambda dt:
                                    self._show_forfeit_popup(f"You gave up! {winner} wins."), 0)
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)

            except Exception as e:
                self._debug(f"[FORFEIT][ERR] {e}")
                Clock.schedule_once(lambda dt:
                                    self._show_forfeit_popup("You gave up! Opponent wins."), 0)
                Clock.schedule_once(lambda dt: self._reset_after_popup(), 2.5)

        threading.Thread(target=worker, daemon=True).start()

    def _show_forfeit_popup(self, msg: str):
        """Show defeat popup (auto-close in 2.5s)."""
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

    def _reset_after_popup(self, *_):
        self._stop_online_sync()
        self._game_active = False
        self._winner_shown = False
        if self.manager:
            self.manager.current = "stage"

    # ---------- Turn timer (Auto-Pass & Auto-Roll) ----------
    def _start_turn_timer(self):
        """Start or resume auto-roll timer with backend-verified 10s idle trigger."""
        self._cancel_turn_timer()
        if not self._game_active:
            return

        # Skip forfeited players
        forfeited = getattr(self, "_forfeited_players", set())
        if self._current_player in forfeited:
            active = [i for i in range(self._num_players) if i not in forfeited]
            if not active:
                self._debug("[TIMER] No active players left — stopping game.")
                self._game_active = False
                return
            self._current_player = active[0]
            self._highlight_turn()

        # --- ONLINE MODE ---
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
                                self._debug(f"[TIMER] Skipped auto-roll (srv_turn={srv_turn}, me={self._my_index})")
                    except Exception as e:
                        self._debug(f"[TIMER][ERR] {e}")

                self._turn_timer = Clock.schedule_once(_verify_and_roll, 10)
            return

        # --- OFFLINE MODE ---
        if self._current_player == 0:
            self._debug("[TIMER] 10s offline auto-roll for player 0")
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

    def _trigger_initial_roll(self):
        """Auto-roll immediately when the game starts (first turn)."""
        if not self._game_active:
            return

        current = self._current_player
        if self._online:
            if current == self._my_index:
                self._debug("[AUTO-ROLL-START] Online mode → first roll (you).")
                self.roll_dice()
            else:
                self._debug("[AUTO-ROLL-START] Online mode → waiting for opponent’s first move.")
        else:
            if current == 0:
                self._debug("[AUTO-ROLL-START] Offline → auto-roll for player 0 (you).")
                self.roll_dice()
            else:
                self._debug("[AUTO-ROLL-START] Offline → bot auto-roll starting.")
                self._auto_roll_current()

    def _auto_roll_real_online(self):
        """Auto-roll after 10s idle in online mode (only if backend confirms turn)."""
        if not self._online or not self._game_active:
            return
        try:
            resp = requests.get(
                f"{self._backend()}/matches/check",
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"match_id": self.match_id},
                timeout=4,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                srv_turn = int(data.get("turn", -1))
                if srv_turn != self._my_index:
                    self._debug(f"[AUTO-ROLL] Skipped, backend turn={srv_turn}, me={self._my_index}")
                    return
        except Exception as e:
            self._debug(f"[AUTO-ROLL][CHECK][ERR] {e}")
            return

        self._debug(f"[AUTO-ROLL] Performing verified auto-roll for player {self._my_index}")
        self._auto_from_timer = True
        self._roll_locked = False
        self._roll_inflight = False
        self._turn_confirmed_once = True

        try:
            if "dice_button" in self.ids:
                dummy = random.randint(1, 6)
                self.ids.dice_button.animate_spin(dummy)
        except Exception:
            pass

        Clock.schedule_once(lambda dt: self.roll_dice(), 0.5)

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
        """Unified handler for rolls, turns, forfeits, spawns, and winners."""
        try:
            positions = payload.get("positions") or self._positions
            roll = payload.get("last_roll")
            actor = payload.get("actor")
            turn = payload.get("turn")
            winner = payload.get("winner")
            forfeit_flag = payload.get("forfeit", False)
            forfeit_actor = payload.get("forfeit_actor")
            spawn = payload.get("spawn", False)

            # --- Hide forfeited player ---
            if forfeit_actor is not None:
                if not hasattr(self, "_forfeited_players"):
                    self._forfeited_players = set()
                if forfeit_actor not in self._forfeited_players:
                    self._forfeited_players.add(forfeit_actor)
                    for prefix in (f"p{forfeit_actor + 1}_pic", f"p{forfeit_actor + 1}_name"):
                        if prefix in self.ids:
                            try:
                                self.ids[prefix].opacity = 0
                            except Exception:
                                pass
                    if forfeit_actor < len(self._coins) and self._coins[forfeit_actor]:
                        self._coins[forfeit_actor].opacity = 0
                    self._show_temp_popup(f"Player {forfeit_actor + 1} gave up!", duration=2)
                    self._debug(f"[FORFEIT] Player {forfeit_actor} hidden from board.")

            # --- Duplicate state filter ---
            sig = (tuple(positions), int(roll or 0), int(turn or -1))
            if getattr(self, "_last_state_sig", None) == sig:
                self._debug("[SYNC] Duplicate state – ignored")
                return
            self._last_state_sig = sig

            # --- Animate dice if rolled ---
            if roll and "dice_button" in self.ids:
                try:
                    self.ids.dice_button.animate_spin(int(roll))
                except Exception:
                    pass

            # --- Spawn Handling (roll == 1) ---
            if spawn and actor is not None:
                self._debug(f"[SPAWN] Player {actor} enters board at box 0 (spawn roll=1)")
                self._spawned_on_board[actor] = True
                self._move_coin_to_box(actor, 0)

                # Realign next turn after spawn so next player can roll manually
                def _realign_after_spawn(*_):
                    try:
                        resp = requests.get(
                            f"{self._backend()}/matches/check",
                            headers={"Authorization": f"Bearer {self._token()}"},
                            params={"match_id": self.match_id},
                            timeout=5,
                            verify=False,
                        )
                        if resp.status_code == 200:
                            srv_turn = int(resp.json().get("turn", -1))
                            if srv_turn != self._current_player:
                                self._current_player = srv_turn
                                self._debug(f"[SPAWN][SYNC] Backend turn → player {self._current_player}")
                    except Exception as e:
                        self._debug(f"[SPAWN][SYNC][ERR] {e}")

                    self._unlock_and_continue()

                Clock.schedule_once(_realign_after_spawn, 0.8)
                return

            # --- Coin movements ---
            old = self._positions[:]
            self._positions = [int(p) for p in positions]
            self._ensure_coin_widgets()
            for i, (a, b) in enumerate(zip(old, positions)):
                if a != b:
                    self._move_coin_to_box(i, b)
                    self._debug(f"[MOVE] Player {i} {a}→{b}")

            # --- Winner ---
            if winner is not None and not forfeit_flag:
                self._debug(f"[WINNER] Player {winner} declared winner")
                Clock.schedule_once(lambda dt: self._declare_winner(int(winner)), 0.8)
                return

            # --- Forfeit logic ---
            if forfeit_flag:
                forfeited = getattr(self, "_forfeited_players", set())
                active = [i for i in range(self._num_players) if i not in forfeited]
                if not active:
                    self._debug("[FORFEIT] All players gone — stopping game.")
                    self._game_active = False
                    return
                if self._current_player in forfeited or self._current_player not in active:
                    self._current_player = active[0]
                    self._debug(f"[FORFEIT] Rotated turn → player {self._current_player}")
                self._highlight_turn()
                Clock.schedule_once(lambda dt: self._start_turn_timer(), 0.6)
                return

            # --- Turn rotation ---
            prev_turn = self._current_player
            forfeited = getattr(self, "_forfeited_players", set())
            active = [i for i in range(self._num_players) if i not in forfeited]

            if turn is not None:
                next_turn = int(turn)
                if next_turn in forfeited or next_turn not in range(self._num_players):
                    next_turn = active[0]
                self._current_player = next_turn
            else:
                next_turn = (actor + 1) % self._num_players
                while next_turn in forfeited:
                    next_turn = (next_turn + 1) % self._num_players
                self._current_player = next_turn

            # --- Unlock for next turn ---
            if prev_turn != self._current_player:
                self._unlock_and_continue()
            else:
                self._debug("[TURN][SYNC] Same player retained — skipping unlock")

        except Exception as e:
            self._debug(f"[SYNC][ERR] {e}")

    def _start_turn_timer(self):
        """Start or resume auto-roll timer with backend-verified 10s idle trigger."""
        self._cancel_turn_timer()
        if not self._game_active:
            return

        # Skip forfeited players
        forfeited = getattr(self, "_forfeited_players", set())
        if self._current_player in forfeited:
            active = [i for i in range(self._num_players) if i not in forfeited]
            if not active:
                self._debug("[TIMER] No active players left — stopping game.")
                self._game_active = False
                return
            self._current_player = active[0]
            self._highlight_turn()

        # --- ONLINE MODE ---
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
                                self._debug(f"[TIMER] Skipped auto-roll (srv_turn={srv_turn}, me={self._my_index})")
                    except Exception as e:
                        self._debug(f"[TIMER][ERR] {e}")

                self._turn_timer = Clock.schedule_once(_verify_and_roll, 10)
            return

        # --- OFFLINE MODE ---
        if self._current_player == 0:
            self._debug("[TIMER] 10s offline auto-roll for player 0")
            self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

    def _unlock_and_continue(self):
        """Safely unlock roll flags, refresh UI, and schedule next turn timer."""
        try:
            self._roll_locked = False
            self._roll_inflight = False
            self._end_turn_pending = False
            self._cancel_turn_timer()

            self._debug(f"[TURN] Ready for next roll (player {self._current_player})")
            self._highlight_turn()

            # --- ONLINE MODE ---
            if self._online:
                if self._current_player == self._my_index:
                    self._debug(f"[TIMER] 10s auto-roll timer (you)")
                    self._turn_timer = Clock.schedule_once(
                        lambda dt: self._auto_roll_real_online(), 10
                    )
                # else: silent skip — do not log or popup
                return

            # --- OFFLINE MODE ---
            if self._current_player == 0:
                self._turn_timer = Clock.schedule_once(lambda dt: self.roll_dice(), 10)

        except Exception as e:
            self._debug(f"[UNLOCK][ERR] {e}")

    # ---------- (deprecated) fallback animator ----------
    def _animate_diff(self, old_positions, new_positions, reverse=False, actor=None, roll=None, spawn=False):
        """Deprecated: kept for compatibility if somebody calls it."""
        try:
            self._debug("[ANIM] Deprecated _animate_diff() called — using unified _on_server_event flow.")
        except Exception:
            pass
