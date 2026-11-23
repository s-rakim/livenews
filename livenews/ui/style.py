from rich.text import Text

AMBER = "#ffb000"
UP = "#2ee86c"
DOWN = "#ff4d4d"
DIM = "#8a7a55"
CYAN = "#5fd7ff"
WHITE = "#ffffff"


def fmt_vol(v: float | None) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}T"
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def signed(v: float | None, fmt: str = "{:+,.2f}", suffix: str = "") -> Text:
    if v is None:
        return Text("-", style=DIM)
    return Text(fmt.format(v) + suffix, style=UP if v > 0 else DOWN if v < 0 else AMBER)


def pct_text(p: float | None) -> Text:
    return signed(p, "{:+.2f}", "%")


def age_str(sec: float | None) -> str:
    if sec is None:
        return "--"
    sec = max(sec, 0)
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{sec / 60:.0f}m"
    if sec < 86400:
        return f"{sec / 3600:.0f}h"
    return f"{sec / 86400:.0f}d"


def price(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"
