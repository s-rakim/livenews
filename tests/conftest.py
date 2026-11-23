import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Watchlists and alerts go to a temp dir, never the real ~/.livenews."""
    monkeypatch.setenv("LIVENEWS_HOME", str(tmp_path))
    for k in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "LIVENEWS_DATA", "LIVENEWS_TRADING_MODE",
              "LIVENEWS_ALLOW_LIVE", "LIVENEWS_WATCHLIST"):
        monkeypatch.delenv(k, raising=False)
