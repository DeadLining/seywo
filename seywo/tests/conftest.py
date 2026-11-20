"""Keep user-level settings isolated from the developer's installed plugin."""
import pytest


@pytest.fixture(autouse=True)
def user_settings_home(tmp_path, monkeypatch):
    from seywo import settings
    home = tmp_path / 'user-settings'
    monkeypatch.setattr(settings, 'settings_home', lambda: home, raising=False)
    return home
