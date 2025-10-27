from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, FadeTransition, Screen
from kivy.animation import Animation
from screens.login_screen import LoginScreen
from screens.register_screen import RegisterScreen
from screens.settings_screen import SettingsScreen
from screens.stage_screen import StageScreen
from screens.dice_game_screen import DiceGameScreen
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
    # kept for compatibility with your existing setup
    user_token: str | None = None
    user_id: int | None = None
    selected_stake: int | None = None  # used by Stage -> UserMatch handoff
    def build(self):
        Builder.load_file('kv/screens.kv')
        self.sm = ScreenManager(transition=FadeTransition())
        self.sm.app = self  # <-- IMPORTANT: let screens access the app via self.manager.app
        self.sm.add_widget(WelcomeScreen(name='welcome'))
        self.sm.add_widget(LoginScreen(name='login'))
        self.sm.add_widget(RegisterScreen(name='register'))
        self.sm.add_widget(SettingsScreen(name='settings'))
        self.sm.add_widget(StageScreen(name='stage'))
        self.sm.add_widget(DiceGameScreen(name='dicegame'))
        # If a UserMatchScreen exists in your project, register it.
        if UserMatchScreen is not None:
            self.sm.add_widget(UserMatchScreen(name='usermatch'))
        self.sm.current = 'welcome'
        return self.sm
    def on_start(self):
        # Load any previously saved auth info (safe no-op if storage is missing)
        if storage:
            tok = storage.get_token()
            if tok:
                self.user_token = tok
            user = storage.get_user() or {}
            uid = user.get("id")
            if isinstance(uid, int):
                self.user_id = uid
        # Existing star animation
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
if __name__ == '__main__':
    DiceApp().run()
