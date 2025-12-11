from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, FadeTransition, Screen
from kivy.animation import Animation
from kivy.properties import NumericProperty
from screens.login_screen import LoginScreen
from screens.register_screen import RegisterScreen
from screens.settings_screen import SettingsScreen
from screens.stage_screen import StageScreen
from screens.dice_game_screen import DiceGameScreen
from screens.forgot_password_screen import ForgotPasswordScreen
from screens.reset_password_screen import ResetPasswordScreen
from urllib.parse import urlparse, parse_qs

# Optional: only if you later add a UserMatchScreen
try:
    from screens.user_match_screen import UserMatchScreen  # type: ignore
except Exception:
    UserMatchScreen = None  # graceful fallback if the screen doesn't exist

# Optional: read locally stored token/user if your utils.storage exists
try:
    from utils import storage  # type: ignore
except Exception:
    storage = None


class WelcomeScreen(Screen):
    pass


class DiceApp(App):
    # default game variables
    user_token: str | None = None
    user_id: int | None = None
    selected_stake: int | None = None
    selected_mode = NumericProperty(2)  # Default 2-player mode

    def build(self):
        Builder.load_file('kv/screens.kv')
        self.sm = ScreenManager(transition=FadeTransition())
        self.sm.app = self  # Allow access to app from screens

        self.sm.add_widget(WelcomeScreen(name='welcome'))
        self.sm.add_widget(LoginScreen(name='login'))
        self.sm.add_widget(RegisterScreen(name='register'))
        self.sm.add_widget(ForgotPasswordScreen(name='forgot_password'))
        self.sm.add_widget(ResetPasswordScreen(name='reset_password'))
        self.sm.add_widget(SettingsScreen(name='settings'))
        self.sm.add_widget(StageScreen(name='stage'))
        self.sm.add_widget(DiceGameScreen(name='dicegame'))

        # Optional user match screen
        if UserMatchScreen is not None:
            self.sm.add_widget(UserMatchScreen(name='usermatch'))

        self.sm.current = 'welcome'
        return self.sm

    def on_start(self):
        # Load previously saved user data if available
        if storage:
            tok = storage.get_token()
            if tok:
                self.user_token = tok
            user = storage.get_user() or {}
            uid = user.get("id")
            if isinstance(uid, int):
                self.user_id = uid

        # Launch background animation
        self.animate_stars(self.sm.get_screen('welcome'))

    def animate_stars(self, screen):
        try:
            anim1 = Animation(y=screen.ids.star1.y + 20, duration=2) + Animation(y=screen.ids.star1.y, duration=2)
            anim2 = Animation(y=screen.ids.star2.y + 15, duration=3) + Animation(y=screen.ids.star2.y, duration=3)
            anim1.repeat = True
            anim2.repeat = True
            anim1.start(screen.ids.star1)
            anim2.start(screen.ids.star2)
        except Exception as e:
            print(f"[Animation Error] {e}")

    def handle_invite_link(self, link: str) -> bool:
        """Parse a dice://join link and jump straight into the lobby."""
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            stake = int(params.get("stake", [0])[0])
            players = int(params.get("players", [2])[0])
        except Exception:
            return False

        if stake <= 0:
            return False

        self.selected_stake = stake
        self.selected_mode = players
        if storage:
            try:
                storage.set_stake_amount(stake)
                storage.set_num_players(players)
            except Exception:
                pass

        local_name = "Player"
        if self.sm.has_screen("stage"):
            stage_screen = self.sm.get_screen("stage")
            if hasattr(stage_screen, "_current_player_name"):
                local_name = stage_screen._current_player_name()

        if self.sm.has_screen("usermatch") and UserMatchScreen is not None:
            match_screen = self.sm.get_screen("usermatch")
            match_screen.start_matchmaking(local_player_name=local_name, amount=stake, mode=players)
            self.sm.current = "usermatch"
            return True
        return False


if __name__ == '__main__':
    DiceApp().run()
