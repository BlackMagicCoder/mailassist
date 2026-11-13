import pytest


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """The CLI opens generated pages in the browser; tests record the call instead."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda uri, *a, **k: opened.append(uri) or True)
    return opened
