"""The NAS terminal UI.

Rendering is a set of pure functions that turn state into a list of coloured
lines, so it can be tested without a terminal. The App class only deals with
the tty: raw mode, the input loop and painting the frame.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import threading
import time

import nasfleet
import naslib
import nasproviders
from naslib import (
    Snapshot,
    append_history,
    fmt_age,
    fmt_countdown,
    fmt_money,
    fmt_tokens,
    load_history,
    prune_history,
    sparkline,
)

SGR = re.compile(r"\x1b\[[0-9;]*m")


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

class Theme:
    def __init__(self, palette):
        self.p = palette
        self._cache: dict = {}

    def style(self, fg=None, bg=None, bold=False, dim=False) -> str:
        key = (fg, bg, bold, dim)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        parts = ["0"]
        if bold:
            parts.append("1")
        if dim:
            parts.append("2")
        if fg:
            parts.append(self.p.fg_seq(fg))
        if bg:
            parts.append(self.p.fg_seq(bg, bg=True))
        seq = "\x1b[" + ";".join(parts) + "m"
        self._cache[key] = seq
        return seq

    @property
    def base(self) -> str:
        return self.style(self.p.fg, self.p.bg)

    def text(self, text: str, fg=None, bg=None, bold=False, dim=False) -> str:
        return self.style(fg, bg, bold, dim) + text + self.base

    def status_colour(self, status: str) -> str:
        return {
            "ok": self.p.ok,
            "warn": self.p.warn,
            "error": self.p.error,
            "unknown": self.p.muted,
        }.get(status, self.p.fg)

    def bar(self, fraction: float, width: int, status: str) -> str:
        fraction = max(0.0, min(1.0, fraction))
        filled = int(round(fraction * width))
        colour = self.status_colour(status)
        return self.text("█" * filled, colour) + self.text("░" * (width - filled), self.p.border)


# --------------------------------------------------------------------------
# Small string helpers
# --------------------------------------------------------------------------

def vlen(text: str) -> int:
    return len(SGR.sub("", text))


def clip(text: str, width: int) -> str:
    if vlen(text) <= width:
        return text
    out = []
    visible = 0
    i = 0
    while i < len(text) and visible < width:
        if text[i] == "\x1b":
            match = SGR.match(text, i)
            if match:
                out.append(match.group())
                i = match.end()
                continue
        out.append(text[i])
        visible += 1
        i += 1
    return "".join(out) + "\x1b[0m"


def fit(text: str, width: int) -> str:
    visible = vlen(text)
    if visible > width:
        return clip(text, width)
    return text + " " * (width - visible)


def centre(text: str, width: int) -> str:
    visible = vlen(text)
    if visible >= width:
        return clip(text, width)
    left = (width - visible) // 2
    return " " * left + text + " " * (width - visible - left)


# --------------------------------------------------------------------------
# Frame composition
# --------------------------------------------------------------------------

def compose(lines: list[str], width: int, height: int, base: str) -> str:
    out = []
    for row in range(height):
        if row < len(lines):
            out.append(base + lines[row] + base + "\x1b[K")
        else:
            out.append(base + "\x1b[K")
    return "\x1b[H" + "\r\n".join(out)


def box(title: str, body: list[str], width: int, theme: Theme,
        selected: bool = False, badge: str = "") -> list[str]:
    """Draw a card. `body` lines may contain SGR colour."""
    border = theme.p.focus if selected else theme.p.selection
    title_col = theme.p.fg if selected else theme.p.muted
    inner = width - 4

    left = theme.text("╭─ ", border)
    if selected:
        left += theme.text("▎ ", theme.p.accent, bold=True)
    left += theme.text(title, title_col, bold=True) + " "
    right = (" " + badge if badge else "") + theme.text(" ─╮", border)
    # fill the gap between title and the right corner
    gap = width - vlen(left) - vlen(right)
    if gap < 0:
        left = clip(left, max(0, width - vlen(right)))
        gap = max(0, width - vlen(left) - vlen(right))
    lines = [left + theme.text("─" * gap, border) + right]

    for row in body:
        lines.append(theme.text("│ ", border) + fit(row, inner) + theme.text(" │", border))
    lines.append(theme.text("╰" + "─" * (width - 2) + "╯", border))
    return lines


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

def _status_dot(theme: Theme, snap: Snapshot) -> str:
    dot = {"ok": "●", "warn": "●", "error": "●", "unknown": "○"}.get(snap.status, "○")
    return theme.text(dot, theme.status_colour(snap.status), bold=True)


def card_body(snap: Snapshot, inner: int, theme: Theme, history: list[float]) -> list[str]:
    lines: list[str] = []

    # Headline: the value that matters, plus the plan/window.
    if snap.error:
        lines.append(theme.text(snap.error, theme.p.error))
    else:
        if snap.used is not None and snap.unit == "$":
            value = fmt_money(snap.used)
        elif snap.remaining is not None:
            value = f"{snap.unit}{snap.remaining:,.2f}"
        elif snap.used is not None:
            value = fmt_tokens(snap.used)
        else:
            value = ""
        label = {"today": "today", "prepaid": "balance", "30d": "30-day spend",
                 "local": "local"}.get(snap.window, snap.window)
        head = theme.text(value, theme.p.fg, bold=True)
        if label:
            head += "  " + theme.text(label, theme.p.muted)
        if snap.confidence == "estimated":
            head += "  " + theme.text("~est", theme.p.muted, dim=True)
        lines.append(head)

    # Progress bar when there is a limit.
    frac = snap.fraction
    if frac is not None:
        bar = theme.bar(frac, min(28, inner - 12), snap.status)
        pct = theme.text(f"{frac * 100:4.1f}%", theme.status_colour(snap.status))
        lines.append(f"{bar} {pct}")

    # Remaining and reset.
    extras = []
    if snap.remaining is not None and snap.fraction is not None:
        extras.append(theme.text("left ", theme.p.muted) + theme.text(fmt_tokens(snap.remaining), theme.p.fg))
    if snap.reset_at:
        extras.append(theme.text("resets ", theme.p.muted) + theme.text(fmt_countdown(snap.reset_at), theme.p.fg))
    if snap.burn_rate:
        extras.append(theme.text("burn ", theme.p.muted) + theme.text(f"{fmt_tokens(snap.burn_rate)}/h", theme.p.fg))
    if extras:
        lines.append("   ".join(extras))

    if snap.summary:
        lines.append(theme.text(snap.summary, theme.p.muted))

    if history:
        spark = theme.text(sparkline(history), theme.p.accent)
        lines.append(theme.text("trend ", theme.p.muted) + spark)

    # A few details inline; the rest live behind Enter.
    for label, value in snap.details[:3]:
        lines.append(theme.text(f"{label:<16}", theme.p.muted) + theme.text(str(value), theme.p.fg))
    return lines


def render_main(state, width: int, height: int) -> list[str]:
    theme = state.theme
    p = theme.p
    lines: list[str] = []

    # Title bar, with the aggregate spend so far.
    brand = theme.text(" NAS ", p.bg, bg=p.accent, bold=True)
    brand += theme.text("  Niru Agent Stats", p.fg, bold=True)
    right_bits = []
    total = sum(s.used for s in state.snapshots if s.used and s.unit == "$")
    if total:
        right_bits.append(theme.text(naslib.fmt_money(total), p.fg, bold=True))
    right_bits.append(theme.text(p.name, p.muted))
    right_bits.append(theme.text(time.strftime("%H:%M"), p.muted))
    right = theme.text("  ·  ", p.border).join(right_bits)
    gap = width - vlen(brand) - vlen(right)
    lines.append(brand + " " * max(1, gap) + right)
    lines.append(theme.text("─" * width, p.border))

    if not state.config.get("enabled"):
        lines.extend(_empty_state(state, width, height - 4))
        lines.append(_footer(state, width))
        return lines

    # Build the cards, then flatten them into rows with a leading indent.
    inner = width - 6
    cards: list[tuple[Snapshot, list[str]]] = []
    for snap in state.snapshots:
        hist = load_history(snap.provider, state.config.get("history_days", 30))
        values = [v for _, v in hist][-24:]
        badge = f"{_status_dot(theme, snap)} {snap.status}" if not snap.error else theme.text("error", p.error)
        cards.append((snap, box(snap.title, card_body(snap, inner, theme, values),
                                width - 2, theme, badge=badge)))

    flat: list[str] = []
    starts: list[int] = []
    for _, block in cards:
        starts.append(len(flat))
        flat.extend(" " + row for row in block)
        flat.append("")

    # Fit into the body: centre when everything fits, else scroll to keep the
    # selected card in view.
    sel = state.selection
    view_h = height - 3  # title bar (2) + footer (1)
    if len(flat) <= view_h:
        origin = (view_h - len(flat)) // 2
        window = [""] * origin + flat
        state.scroll = 0
    else:
        if 0 <= sel < len(starts):
            block_end = starts[sel] + len(cards[sel][1]) + 1
            if starts[sel] < state.scroll:
                state.scroll = starts[sel]
            elif block_end > state.scroll + view_h:
                state.scroll = block_end - view_h
            state.scroll = max(0, state.scroll)
        else:
            state.scroll = 0
        window = flat[state.scroll:state.scroll + view_h]
        origin = -state.scroll
    window += [""] * (view_h - len(window))

    # Redraw the selected card with the focus border.
    if 0 <= sel < len(cards):
        snap, _ = cards[sel]
        hist = load_history(snap.provider, state.config.get("history_days", 30))
        values = [v for _, v in hist][-24:]
        badge = f"{_status_dot(theme, snap)} {snap.status}" if not snap.error else theme.text("error", p.error)
        block = box(snap.title, card_body(snap, inner, theme, values), width - 2, theme,
                    selected=True, badge=badge)
        rel = origin + starts[sel]
        for offset, row in enumerate(block):
            pos = rel + offset
            if 0 <= pos < len(window):
                window[pos] = " " + row

    lines.extend(window)
    lines.append(_footer(state, width))
    return lines


def _empty_state(state, width: int, height: int) -> list[str]:
    theme = state.theme
    out = []
    msg = theme.text("No providers yet", theme.p.fg, bold=True)
    hint = theme.text("Press ", theme.p.muted) + theme.text("a", theme.p.accent, bold=True) + \
        theme.text(" to add one, or run ", theme.p.muted) + theme.text("nas add <name>", theme.p.accent)
    out.append("")
    out.append(centre(msg, width))
    out.append(centre(hint, width))
    out.append("")
    detected = [p for p in nasproviders.PROVIDERS if p.status()[0] == "ready"]
    if detected:
        out.append(centre(theme.text(f"Detected here: {', '.join(p.title for p in detected)}", theme.p.muted), width))
    pad = max(0, height - len(out))
    out.extend([""] * pad)
    return out


def _footer(state, width: int) -> str:
    theme = state.theme
    p = theme.p
    parts = [
        theme.text("a", p.accent, bold=True) + theme.text(" add", p.muted),
        theme.text("d", p.accent, bold=True) + theme.text(" remove", p.muted),
        theme.text("↵", p.accent, bold=True) + theme.text(" details", p.muted),
        theme.text("r", p.accent, bold=True) + theme.text(" refresh", p.muted),
        theme.text("?", p.accent, bold=True) + theme.text(" help", p.muted),
        theme.text("q", p.accent, bold=True) + theme.text(" quit", p.muted),
    ]
    left = "  ".join(parts)
    if state.loading:
        right = theme.text("refreshing…", p.warn)
    elif state.host_errors:
        right = theme.text(f"{len(state.host_errors)} host(s) unreachable", p.warn)
    elif state.message:
        right = theme.text(state.message, p.muted)
    else:
        right = theme.text(f"updated {fmt_age(state.last_refresh)}", p.muted)
    gap = width - vlen(left) - vlen(right) - 1
    line = left + " " * max(1, gap) + right
    return line


def _overlay(state, width: int, height: int, title: str, rows: list[str],
             hint: str = "") -> list[str]:
    theme = state.theme
    inner = min(width - 6, 76)
    box_width = inner + 2
    body = rows[: max(1, height - 6)]
    block = box(title, body, box_width, theme, selected=True)
    top = max(0, (height - len(block)) // 2)
    left = max(0, (width - box_width) // 2)
    pad = " " * left

    base = render_main_backdrop(state, width, height)
    out = (list(base) + [""] * height)[:height]
    for offset, line in enumerate(block):
        pos = top + offset
        if 0 <= pos < height:
            out[pos] = pad + line
    if hint and 0 <= top - 1 < height:
        out[top - 1] = " " * left + theme.text(hint, theme.p.muted)
    return out


def render_main_backdrop(state, width: int, height: int) -> list[str]:
    # The main view, dimmed, used as the backdrop for overlays.
    lines = render_main(state, width, height)
    return lines


def render_add(state, width: int, height: int) -> list[str]:
    theme = state.theme
    rows = []
    enabled = set(state.config.get("enabled", []))
    for idx, provider in enumerate(nasproviders.PROVIDERS):
        status, detail = provider.status()
        mark = "[x]" if provider.key in enabled else "[ ]"
        cursor = "▸" if idx == state.add_index else " "
        mark_col = theme.p.ok if provider.key in enabled else theme.p.border
        row = theme.text(f"{cursor} ", theme.p.accent) + theme.text(mark, mark_col, bold=True) + " "
        row += theme.text(f"{provider.title:<14}", theme.p.fg, bold=(idx == state.add_index))
        kind = theme.text(f"{provider.kind:<6}", theme.p.muted)
        badge_col = theme.p.ok if status == "ready" else (theme.p.warn if status == "needs-key" else theme.p.border)
        badge = theme.text(status, badge_col)
        row += kind + badge + "  " + theme.text(detail, theme.p.muted)
        rows.append(row)
    hint = "space/↵ toggle · esc done"
    return _overlay(state, width, height, "Add providers", rows, hint)


def render_detail(state, width: int, height: int) -> list[str]:
    theme = state.theme
    if not state.snapshots:
        return _overlay(state, width, height, "Details", ["nothing selected"])
    snap = state.snapshots[min(state.selection, len(state.snapshots) - 1)]
    rows = [
        theme.text(snap.title, theme.p.fg, bold=True),
        theme.text(f"plan    {snap.plan or '-'}", theme.p.muted),
        theme.text(f"source  {snap.source or '-'}", theme.p.muted),
        theme.text(f"window  {snap.window or '-'}    unit {snap.unit or '-'}    confidence {snap.confidence}", theme.p.muted),
        "",
    ]
    if snap.error:
        rows.append(theme.text(snap.error, theme.p.error))
    for label, value in snap.details:
        rows.append(theme.text(f"{label:<20}", theme.p.muted) + theme.text(str(value), theme.p.fg))
    return _overlay(state, width, height, "Details", rows, "esc close")


def render_help(state, width: int, height: int) -> list[str]:
    theme = state.theme
    keys = [
        ("↑ ↓ / j k", "move between providers"),
        ("↵", "show all details for the selected provider"),
        ("a", "add or remove providers"),
        ("d", "remove the selected provider"),
        ("r", "refresh now"),
        ("?", "this help"),
        ("q", "quit"),
    ]
    rows = []
    for key, desc in keys:
        rows.append(theme.text(f"{key:<12}", theme.p.accent, bold=True) + theme.text(desc, theme.p.fg))
    rows.append("")
    rows.append(theme.text("Theme follows the active NIRUPI palette. Override with", theme.p.muted))
    rows.append(theme.text("  nas --theme NAME   (nas --themes to list)", theme.p.muted))
    return _overlay(state, width, height, "NAS - help", rows, "esc close")


# --------------------------------------------------------------------------
# The interactive app
# --------------------------------------------------------------------------

class State:
    def __init__(self, cfg, theme):
        self.config = cfg
        self.theme = theme
        self.snapshots: list[Snapshot] = []
        self.host_errors: list[tuple[str, str]] = []
        self.loading = False
        self.selection = 0
        self.scroll = 0
        self.view = "main"
        self.add_index = 0
        self.message = ""
        self.last_refresh = 0.0


class App:
    def __init__(self, cfg, theme, use_fleet: bool = False):
        self.state = State(cfg, theme)
        self._size = (80, 24)
        self._stop = False
        self._fleet = use_fleet
        self._lock = threading.Lock()       # protects state.snapshots
        self._draw = threading.Lock()       # serialises writes to the terminal
        self._fetching = False
        self._resize = False
        self._active = False

    # -- tty plumbing ------------------------------------------------------
    def _enter(self):
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        self._active = True

    def _leave(self):
        import termios
        self._active = False
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def _size_update(self):
        self._size = shutil.get_terminal_size((80, 24))

    def _paint(self):
        if not self._active:
            return
        width, height = self._size
        state = self.state
        with self._lock:
            if state.view == "add":
                lines = render_add(state, width, height)
            elif state.view == "help":
                lines = render_help(state, width, height)
            elif state.view == "detail":
                lines = render_detail(state, width, height)
            else:
                lines = render_main(state, width, height)
            frame = compose(lines, width, height, state.theme.base)
        with self._draw:
            sys.stdout.write(frame)
            sys.stdout.flush()

    # -- data --------------------------------------------------------------
    def _apply(self, snaps, errors):
        for snap in snaps:
            append_history(snap)
        naslib.prune_history(self.state.config.get("history_days", 30))
        with self._lock:
            self.state.snapshots = snaps
            self.state.host_errors = errors
            self.state.loading = False
            self.state.last_refresh = time.time()
        self._paint()

    def _refresh(self, block: bool = False):
        if self._fetching and not block:
            return

        def work():
            self._fetching = True
            with self._lock:
                self.state.loading = True
            self._paint()
            try:
                local = nasproviders.fetch_all(self.state.config)
            except Exception as exc:  # noqa: BLE001
                local = []
                self.state.message = str(exc)
            # Local first, so the cards appear instantly.
            self._apply(local, [])
            # Then the fleet, merged in.
            if self._fleet and nasfleet.fleet_hosts():
                with self._lock:
                    self.state.loading = True
                self._paint()
                try:
                    remote, errors = nasfleet.gather_remote(self.state.config)
                    merged = nasfleet.merge(self.state.config, local, remote)
                except Exception as exc:  # noqa: BLE001
                    merged, errors = local, []
                    self.state.message = str(exc)
                self._apply(merged, errors)
            self._fetching = False

        if block:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    # -- input -------------------------------------------------------------
    def _read_key(self, timeout: float):
        """Read one key. After a lone ESC, wait briefly for the rest of a
        sequence so arrow keys are not mistaken for the escape key."""
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        data = os.read(fd, 1)
        if data == b"\x1b":
            while len(data) < 3:
                more, _, _ = select.select([sys.stdin], [], [], 0.02)
                if not more:
                    break
                chunk = os.read(fd, 8)
                if not chunk:
                    break
                data += chunk
            return data
        while True:
            more, _, _ = select.select([sys.stdin], [], [], 0)
            if not more:
                break
            data += os.read(fd, 8)
        return data

    def run(self) -> int:
        self._size_update()
        self._enter()
        old_winch = signal.getsignal(signal.SIGWINCH)

        def on_winch(signum, frame):
            # Never paint from a signal handler; just ask the loop to.
            self._resize = True

        signal.signal(signal.SIGWINCH, on_winch)
        try:
            self._refresh(block=True)
            while not self._stop:
                interval = max(5, int(self.state.config.get("refresh_seconds", 60) or 60))
                key = self._read_key(1.0)
                if self._resize:
                    self._resize = False
                    self._size_update()
                    self._paint()
                if key is not None:
                    self._handle(key)
                if time.time() - self.state.last_refresh >= interval and self.state.view == "main":
                    self.state.message = ""
                    self._refresh(block=False)
                if self.state.view == "main" and not self.state.loading:
                    self._paint()
        finally:
            signal.signal(signal.SIGWINCH, old_winch)
            self._leave()
        return 0

    def _handle(self, data: bytes):
        state = self.state
        text = data.decode("utf-8", "replace")
        if text in ("\x03", "\x04"):  # ctrl-c / ctrl-d
            self._stop = True
            return
        if state.view == "main":
            self._handle_main(data, text)
        elif state.view == "add":
            self._handle_add(data, text)
        elif state.view in ("help", "detail"):
            if data in (b"\x1b", b"q") or text in ("q",):
                state.view = "main"
            elif state.view == "detail" and data in (b"\x1b[B", b"j"):
                pass
            self._paint()

    def _move(self, delta: int):
        state = self.state
        if state.snapshots:
            state.selection = max(0, min(len(state.snapshots) - 1, state.selection + delta))

    def _handle_main(self, data: bytes, text: str):
        state = self.state
        if data in (b"q",):
            self._stop = True
        elif data in (b"j", b"\x1b[B", b"\n", b"\r") or text == "\x1b[B":
            if data in (b"\n", b"\r"):
                if state.snapshots:
                    state.view = "detail"
            else:
                self._move(1)
        elif data in (b"k", b"\x1b[A"):
            self._move(-1)
        elif data == b"\x1b[C":
            pass
        elif data == b"r":
            state.message = ""
            self._refresh(block=False)
        elif data == b"a":
            state.view = "add"
            state.add_index = 0
        elif data == b"d":
            self._remove_selected()
        elif data in (b"?", b"h"):
            state.view = "help"
        self._paint()

    def _remove_selected(self):
        state = self.state
        if not state.snapshots:
            return
        key = state.snapshots[state.selection].provider
        enabled = [k for k in state.config.get("enabled", []) if k != key]
        state.config["enabled"] = enabled
        naslib.save_config(state.config)
        state.selection = max(0, state.selection - 1)
        state.message = f"removed {key}"
        self._refresh(block=False)

    def _handle_add(self, data: bytes, text: str):
        state = self.state
        if data == b"\x1b":
            state.view = "main"
            self._paint()
            return
        if data in (b"\x1b[B", b"j"):
            state.add_index = min(len(nasproviders.PROVIDERS) - 1, state.add_index + 1)
        elif data in (b"\x1b[A", b"k"):
            state.add_index = max(0, state.add_index - 1)
        elif data in (b" ", b"\n", b"\r"):
            provider = nasproviders.PROVIDERS[state.add_index]
            enabled = list(state.config.get("enabled", []))
            if provider.key in enabled:
                enabled.remove(provider.key)
            else:
                enabled.append(provider.key)
            state.config["enabled"] = enabled
            naslib.save_config(state.config)
            self._refresh(block=False)
        self._paint()
