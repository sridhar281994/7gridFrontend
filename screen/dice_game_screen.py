# --- dice_game_screen.py (full) ---

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
        self.stop_spin()
        anim_up = (
            Animation(_rotation_angle=360, d=0.35, t="linear") +
            Animation(_rotation_angle=720, d=0.35, t="linear")
        )
        zoom = (
            Animation(scale_value=1.3, d=0.15, t="out_quad") +
            Animation(scale_value=1.0, d=0.15, t="out_quad")
        )
        anim_up.bind(on_progress=lambda *_: self._sync_rotation())
        zoom.start(self)
        anim_up.start(self)
        self._anim = anim_up

        def set_face(*_):
            img_path = os.path.join("assets", "dice", f"dice{result}.png")
            if os.path.exists(img_path):
                self._dice_image.source = img_path
                self._dice_image.reload()
            self.stop_spin()
            self._rotation_angle = 0
            self._sync_rotation()

        anim_up.bind(on_complete=set_face)

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
        self._online = bool(mid)
        self.match_id = mid
        if self._online:
            self._start_online_sync()
        else:
            self._highlight_turn()

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

        pulse(p1_overlay, self._current_player == 0)
        pulse(p2_overlay, self._current_player == 1)
        pulse(p3_overlay, self._current_player == 2 and bool(self.player3_name))

        # 🔹 Start 10s auto-turn timer in OFFLINE only
        self._start_turn_timer()

    # ---------- dice ----------
    def roll_dice(self):
        """Request dice roll from backend (server authoritative)."""
        if not self._game_active or not self._online or not self.match_id or not self._token():
            return

        # ✅ Prevent roll if not your turn
        if self._current_player != self._my_index:
            self._debug("[TURN] Not your turn!")
            self._show_temp_popup("⏳ Not your turn!", duration=1.5)
            return

        if getattr(self, "_roll_inflight", False):
            self._debug("[ROLL] Already waiting for server")
            return
        self._roll_inflight = True

        def worker():
            try:
                resp = requests.post(
                    f"{self._backend()}/matches/roll",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    json={"match_id": self.match_id},
                    timeout=10,
                    verify=False
                )

                if resp.status_code == 200:
                    data = resp.json()
                    roll = int(data.get("roll") or 1)
                    Clock.schedule_once(lambda dt: self._animate_dice_and_apply_server(data, roll), 0)
                elif resp.status_code == 409:  # backend says not your turn
                    self._debug("[TURN] Server rejected roll — not your turn")
                    Clock.schedule_once(lambda dt: self._show_temp_popup("⏳ Not your turn!", duration=1.5), 0)
            except Exception as e:
                self._debug(f"[ROLL][ERR] {e}")
            finally:
                self._roll_inflight = False

        threading.Thread(target=worker, daemon=True).start()

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
        """Offline dice roll logic. Server handles this in online mode."""
        if self._online:
            self._debug("[SKIP] Online mode active — backend handles dice roll.")
            return

        p = self._current_player
        old = self._positions[p]
        new_pos = old + roll
        BOARD_MAX = 7
        DANGER_BOX = 3

        self._debug(f"[OFFLINE] Player {p} rolled {roll} (from {old})")

        # spawn only on 1
        if not self._spawned_on_board[p]:
            if roll == 1:
                self._spawned_on_board[p] = True
                self._positions[p] = 0
                self._move_coin_to_box(p, 0)
                self._debug(f"[SPAWN] Player {p} enters board at box 0")
            else:
                self._positions[p] = 0
                self._debug(f"[SKIP] Player {p} not spawned (roll={roll})")
            self._end_turn_and_highlight()
            return

        # reverse at 3
        if new_pos == DANGER_BOX:
            self._debug(f"[DANGER] Player {p} hit box {DANGER_BOX} → reset to start")
            self._positions[p] = 0
            self._move_coin_to_box(p, DANGER_BOX)
            Clock.schedule_once(lambda dt: self._move_coin_to_box(p, 0, reverse=True), 0.5)
            Clock.schedule_once(lambda dt: self._end_turn_and_highlight(), 0.6)
            return

        # exact win
        if new_pos == BOARD_MAX:
            self._positions[p] = BOARD_MAX
            self._move_coin_to_box(p, BOARD_MAX)
            self._declare_winner(p)
            return

        # overshoot
        if new_pos > BOARD_MAX:
            self._debug(f"[OVERSHOOT] Player {p} rolled {roll} → stays at {old}")
            self._positions[p] = old
            self._move_coin_to_box(p, old)
            self._end_turn_and_highlight()
            return

        # normal move
        self._positions[p] = new_pos
        self._move_coin_to_box(p, new_pos)
        self._debug(f"[MOVE] Player {p} moved to box {new_pos}")

        self._end_turn_and_highlight()

    def _end_turn_and_highlight(self):
        self._current_player = (self._current_player + 1) % self._num_players
        self._highlight_turn()

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

        def worker():
            try:
                resp = requests.post(
                    f"{backend}/matches/forfeit",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"match_id": match_id},
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

                # --- Local player popup ---
                Clock.schedule_once(lambda dt: self._show_forfeit_popup(f"You gave up! {winner} wins."), 0)
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
        Start a 10-second timer for each turn.
        - In ONLINE mode → auto-pass to next player if inactive.
        - In OFFLINE mode → auto-roll dice for inactive player.
        """
        self._cancel_turn_timer()
        if not self._game_active:
            return

        self._debug(f"[TIMER] Started 10s timer for player {self._current_player}")
        if self._online:
            # Auto-pass to next player after 10s of inactivity
            self._turn_timer = Clock.schedule_once(lambda dt: self._auto_pass_turn(), 10)
        else:
            # Offline: auto-roll the dice after 10s
            self._turn_timer = Clock.schedule_once(lambda dt: self._auto_roll_current(), 10)

    def _cancel_turn_timer(self):
        """Cancel any active turn timer."""
        if hasattr(self, "_turn_timer") and self._turn_timer:
            try:
                self._turn_timer.cancel()
            except Exception:
                pass
            self._turn_timer = None

    def _auto_roll_current(self):
        """Auto-roll if player doesn't act within 10s (OFFLINE mode only)."""
        if self._online or not self._game_active:
            return
        self._debug(f"[AUTO] 10s timeout → auto roll for player {self._current_player}")
        try:
            roll = random.randint(1, 6)
            self._debug(f"[AUTO] Offline auto roll = {roll}")
            self._apply_roll(roll)
        except Exception as e:
            self._debug(f"[AUTO][ERR] {e}")

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
    # ---------- ONLINE EVENT HANDLER ----------
    def _on_server_event(self, payload: dict):
        """Handle real-time updates from backend (positions, roll, turn, winner, forfeit)."""
        try:
            # --- Extract all relevant fields ---
            positions = payload.get("positions") or self._positions
            roll = payload.get("last_roll")
            actor = payload.get("actor")
            reverse = bool(payload.get("reverse", False))
            spawn = bool(payload.get("spawn", False))
            winner = payload.get("winner")
            turn = payload.get("turn")
            finished = payload.get("finished", False)
            forfeit_flag = payload.get("forfeit", False)

            # --- Dedup identical state ---
            sig = (tuple(positions), int(roll) if roll else None, int(turn) if turn is not None else None)
            if getattr(self, "_last_state_sig", None) == sig:
                self._debug("[SYNC] Duplicate state – ignored")
                return
            self._last_state_sig = sig

            # --- Dice animation ---
            if roll and "dice_button" in self.ids:
                self._debug(f"[ROLL] Actor={actor} rolled={roll}")
                self.ids.dice_button.animate_spin(int(roll))

            # --- Detect who moved ---
            if actor is None:
                diffs = [i for i, (a, b) in enumerate(zip(self._positions, positions)) if a != b]
                actor = diffs[0] if diffs else 0

            old_positions = self._positions[:]
            self._positions = [int(p) for p in positions]
            self._ensure_coin_widgets()

            # --- Spawn ---
            if spawn and actor is not None:
                self._debug(f"[SPAWN] Player {actor} enters board at 0")
                self._move_coin_to_box(actor, 0)
                self._spawned_on_board[actor] = True
                return

            # --- Reverse ---
            if reverse and actor is not None:
                self._debug(f"[REVERSE] Player {actor} returning to 0")
                Clock.schedule_once(lambda dt: self._move_coin_to_box(actor, 0, reverse=True), 0.6)
                return

            # --- Normal movement ---
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

            # --- Winner ---
            if winner is not None:
                Clock.schedule_once(lambda dt: self._declare_winner(int(winner)), 0.8)
                return

            # --- Forfeit or Finished ---
            if forfeit_flag or finished:
                self._debug("[FORFEIT] Match ended — returning to stage screen.")
                msg = (
                    "Opponent gave up! You win!"
                    if (winner is None or winner == self._my_index)
                    else "Match ended by opponent."
                )
                popup = Popup(
                    title="Match Over",
                    content=Label(text=msg, halign="center"),
                    size_hint=(None, None),
                    size=(300, 200),
                    auto_dismiss=False,
                )
                popup.open()

                # Auto-close the popup after 2.5s and return to stage
                def close_popup(dt):
                    if popup:
                        popup.dismiss()
                    self._reset_after_popup()

                Clock.schedule_once(close_popup, 2.5)
                return

            # --- Update current turn ---
            if turn is not None:
                try:
                    self._current_player = int(turn)
                except Exception:
                    pass
            self._highlight_turn()

        except Exception as e:
            self._debug(f"[SYNC][ERR] {e}")

    # ---------- (deprecated) fallback animator ----------
    def _animate_diff(self, old_positions, new_positions, reverse=False, actor=None, roll=None, spawn=False):
        """Deprecated: kept for compatibility if somebody calls it."""
        try:
            self._debug("[ANIM] Deprecated _animate_diff() called — using unified _on_server_event flow.")
        except Exception:
            pass
