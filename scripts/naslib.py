"""Core library for NAS - Niru Agent Stats.

Dependency-free: Python 3.10+ standard library only, so it runs the same on
Debian, Arch/Omarchy and Void. Everything that is not presentation lives here:
configuration, the NIRUPI theme palette, colour maths and the normalised usage
snapshot that every provider returns.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.1.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "nas"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "nas"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "enabled": [],                 # only these providers are shown
    "refresh_seconds": 60,         # auto-refresh interval
    "theme": "auto",               # "auto" follows NIRUPI; or a palette name
    "truecolor": "auto",           # auto | on | off
    "history_days": 30,            # keep this many days of snapshots
    "fleet": "auto",               # "auto" = merge the NIRUPI fleet.conf hosts
    "providers": {},               # per-provider settings, e.g. api key env var
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        cfg.update(data)
    if not isinstance(cfg.get("enabled"), list):
        cfg["enabled"] = []
    if not isinstance(cfg.get("providers"), dict):
        cfg["providers"] = {}
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=False) + "\n")
    tmp.replace(CONFIG_FILE)


def provider_setting(cfg: dict, key: str, name: str, default=None):
    return (cfg.get("providers", {}).get(key, {}) or {}).get(name, default)


# --------------------------------------------------------------------------
# Colour maths
# --------------------------------------------------------------------------

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except (ValueError, IndexError):
        return (255, 255, 255)


# The 16 base ANSI colours, used when the terminal only does 16 colours.
_ANSI16 = [
    (0x00, 0x00, 0x00), (0x80, 0x00, 0x00), (0x00, 0x80, 0x00), (0x80, 0x80, 0x00),
    (0x00, 0x00, 0x80), (0x80, 0x00, 0x80), (0x00, 0x80, 0x80), (0xc0, 0xc0, 0xc0),
    (0x80, 0x80, 0x80), (0xff, 0x00, 0x00), (0x00, 0xff, 0x00), (0xff, 0xff, 0x00),
    (0x00, 0x00, 0xff), (0xff, 0x00, 0xff), (0x00, 0xff, 0xff), (0xff, 0xff, 0xff),
]


def _dist2(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def rgb_to_ansi16(rgb: tuple[int, int, int]) -> int:
    return min(range(16), key=lambda i: _dist2(rgb, _ANSI16[i]))


def rgb_to_xterm256(rgb: tuple[int, int, int]) -> int:
    # Greyscale ramp and the 6x6x6 colour cube, whichever is closer.
    r, g, b = rgb
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + (r - 8) // 10
    cube = [0, 95, 135, 175, 215, 255]

    def nearest(v: int) -> int:
        return min(range(6), key=lambda i: abs(cube[i] - v))

    ri, gi, bi = nearest(r), nearest(g), nearest(b)
    return 16 + 36 * ri + 6 * gi + bi


def detect_colour_depth(pref: str = "auto") -> str:
    """Return 'true', '256' or '16'."""
    if pref == "on":
        return "true"
    if pref == "off":
        return "16"
    colourterm = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "").lower()
    if colourterm in ("truecolor", "24bit"):
        return "true"
    if "256color" in term or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm"):
        return "256"
    if term in ("", "dumb"):
        return "16"
    return "256"


# --------------------------------------------------------------------------
# NIRUPI palette
# --------------------------------------------------------------------------

@dataclass
class Palette:
    name: str = "Noir"
    depth: str = "256"
    bg: str = "#0f0f10"
    fg: str = "#e5e5e5"
    muted: str = "#a8a8a8"
    border: str = "#2a2a2d"
    focus: str = "#5a5a60"
    selection: str = "#3a3a3d"
    ansi: list[str] = field(default_factory=list)

    def fg_seq(self, colour: str, *, bg: bool = False) -> str:
        r, g, b = hex_to_rgb(colour)
        head = "48" if bg else "38"
        if self.depth == "true":
            return f"{head};2;{r};{g};{b}"
        if self.depth == "256":
            return f"{head};5;{rgb_to_xterm256((r, g, b))}"
        return f"{'4' if bg else '3'}{rgb_to_ansi16((r, g, b))}"

    # Semantic accents, taken from the palette's ANSI ramp so they match dwm.
    @property
    def ok(self) -> str:
        return self._ansi(10)

    @property
    def warn(self) -> str:
        return self._ansi(11)

    @property
    def error(self) -> str:
        return self._ansi(9)

    @property
    def info(self) -> str:
        return self._ansi(12)

    @property
    def accent(self) -> str:
        return self._ansi(4)

    def _ansi(self, index: int) -> str:
        if 0 <= index < len(self.ansi) and self.ansi[index]:
            return self.ansi[index]
        return self.fg


def _nirupi_theme_dir() -> Path | None:
    candidates = [
        Path.home() / ".local" / "lib" / "nirupi" / "noir" / "theme",
    ]
    base = Path.home() / ".local" / "lib" / "nirupi"
    if base.exists():
        for path in sorted(base.glob("*/noir/theme")):
            candidates.append(path)
        for path in sorted(base.glob("*/*/theme")):
            candidates.append(path)
    for path in candidates:
        if path.is_dir():
            return path
    return None


def _active_tokens_file() -> Path | None:
    env = os.environ.get("NIRUPI_TOKENS_FILE")
    if env and Path(env).is_file():
        return Path(env)
    user = CONFIG_DIR.parent / "nirupi" / "tokens.env"
    if user.is_file() and not user.is_symlink():
        return user
    for candidate in (
        Path.home() / ".config" / "nirupi" / "tokens.env",
        Path.home() / ".local" / "lib" / "nirupi" / "noir" / "theme" / "tokens.env",
    ):
        if candidate.is_file():
            return candidate
    theme_dir = _nirupi_theme_dir()
    if theme_dir and (theme_dir / "tokens.env").is_file():
        return theme_dir / "tokens.env"
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _palette_from_env(data: dict[str, str], name: str, depth: str) -> Palette:
    def get(key: str, default: str) -> str:
        value = data.get(key, "").lstrip("#")
        return f"#{value}" if value else default

    fg_fallback = get("NOIR_FG", "#e5e5e5")
    ansi = [get(f"NOIR_ANSI_{i}", "") or fg_fallback for i in range(16)]
    return Palette(
        name=name,
        depth=depth,
        bg=get("NOIR_BG", "#0f0f10"),
        fg=fg_fallback,
        muted=get("NOIR_MUTED", "#a8a8a8"),
        border=get("NOIR_BORDER", "#2a2a2d"),
        focus=get("NOIR_FOCUS", "#5a5a60"),
        selection=get("NOIR_SELECTION", "#3a3a3d"),
        ansi=ansi,
    )


def load_palette(theme: str = "auto", truecolor: str = "auto") -> Palette:
    """Load a NIRUPI palette. 'auto' follows the active theme."""
    depth = detect_colour_depth(truecolor)
    tokens = _active_tokens_file()
    if tokens is None:
        return Palette(depth=depth)
    data = _parse_env_file(tokens)
    if theme and theme != "auto":
        palette_dir = tokens.parent
        target = "tokens.env" if theme.lower() == "noir" else f"{theme}.env"
        candidate = palette_dir / target
        if not candidate.is_file():
            for alt in palette_dir.glob("*.env"):
                if alt.stem.lower() == theme.lower():
                    candidate = alt
                    break
        if candidate.is_file():
            data = _parse_env_file(candidate)
    name = data.get("NOIR_THEME_NAME") or ("Noir" if theme == "auto" else theme)
    return _palette_from_env(data, name, depth)


def list_themes() -> list[str]:
    theme_dir = _nirupi_theme_dir()
    if not theme_dir:
        return ["noir"]
    names = ["noir"]
    for path in sorted(theme_dir.glob("*.env")):
        if path.stem != "tokens":
            names.append(path.stem)
    return names


# --------------------------------------------------------------------------
# Usage snapshot - the normalised shape every provider returns
# --------------------------------------------------------------------------

@dataclass
class Snapshot:
    """One card of usage for one provider."""

    provider: str
    title: str
    summary: str = ""
    plan: str = ""
    window: str = ""                 # "5h", "weekly", "monthly", "prepaid", "local"
    unit: str = ""                   # "$", "tokens", "requests"
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    reset_at: float | None = None    # epoch seconds
    burn_rate: float | None = None   # unit per hour
    confidence: str = "exact"        # exact | estimated | unknown
    source: str = ""
    details: list[tuple[str, str]] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)   # structured, for fleet merging
    sessions: int = 0
    error: str | None = None
    status: str = "ok"               # ok | warn | error | unknown

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        """Rebuild a snapshot from the JSON emitted by `nas --json`."""
        snap = cls(provider=data.get("provider", ""), title=data.get("title", ""))
        for name in ("summary", "plan", "window", "unit", "confidence", "source", "error"):
            if data.get(name) is not None:
                setattr(snap, name, data[name])
        for name in ("used", "limit", "remaining", "reset_at", "burn_rate"):
            setattr(snap, name, data.get(name))
        snap.details = [tuple(x) for x in (data.get("details") or [])]
        snap.tokens = data.get("tokens") or {}
        snap.sessions = int(data.get("sessions") or 0)
        if data.get("status"):
            snap.status = data["status"]
        snap.recompute_status()
        return snap

    @property
    def fraction(self) -> float | None:
        if self.used is not None and self.limit:
            return max(0.0, min(1.0, self.used / self.limit))
        return None

    def recompute_status(self) -> None:
        if self.error:
            self.status = "error"
            return
        frac = self.fraction
        if frac is not None:
            if frac >= 0.95:
                self.status = "error"
            elif frac >= 0.8:
                self.status = "warn"
            else:
                self.status = "ok"
        elif self.used is None and self.remaining is None:
            self.status = "unknown"
        elif self.status not in ("warn", "error"):
            # A balance or a limit-less usage figure: keep any manual warning.
            self.status = "ok"


def fmt_tokens(n: float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}"


def fmt_money(n: float | None, unit: str = "$") -> str:
    if n is None:
        return "-"
    return f"{unit}{n:,.2f}" if abs(n) >= 0.01 else f"{unit}{n:,.4f}"


def fmt_countdown(epoch: float | None) -> str:
    if not epoch:
        return "-"
    delta = int(epoch - time.time())
    if delta <= 0:
        return "now"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_age(epoch: float | None) -> str:
    if not epoch:
        return "-"
    delta = int(time.time() - epoch)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --------------------------------------------------------------------------
# History (for sparklines and burn rate)
# --------------------------------------------------------------------------

def append_history(snapshot: Snapshot) -> None:
    if snapshot.used is None:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "t": int(time.time()),
        "provider": snapshot.provider,
        "used": snapshot.used,
        "limit": snapshot.limit,
        "unit": snapshot.unit,
    }
    try:
        with HISTORY_FILE.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_history(provider: str, days: int = 30) -> list[tuple[int, float]]:
    cutoff = int(time.time()) - days * 86400
    points: list[tuple[int, float]] = []
    try:
        with HISTORY_FILE.open() as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("provider") == provider and rec.get("t", 0) >= cutoff:
                    points.append((int(rec["t"]), float(rec["used"])))
    except OSError:
        return []
    points.sort()
    return points


def prune_history(days: int) -> None:
    cutoff = int(time.time()) - days * 86400
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except OSError:
        return
    kept = []
    for line in lines:
        try:
            if json.loads(line).get("t", 0) >= cutoff:
                kept.append(line)
        except ValueError:
            continue
    try:
        HISTORY_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))
    except OSError:
        pass


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi <= lo:
        return blocks[0] * len(values)
    return "".join(blocks[int((v - lo) / (hi - lo) * (len(blocks) - 1))] for v in values)
