"""Providers for NAS.

Each provider is a small, self-contained source that turns one place where
agent usage is recorded into a normalised naslib.Snapshot. Adding a new one is
just a matter of writing a `fetch` + `detect` and appending it to PROVIDERS.

Local providers read files already on disk (no network, no keys). API providers
use an official endpoint and need a key, taken from the environment, the NAS
config, or ~/.config/nas/keys.env.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from naslib import (
    Snapshot,
    fmt_tokens,
    load_config,
    provider_setting,
    sparkline,
)

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _http_json(url: str, headers: dict, timeout: int = 15):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _keys_file() -> dict[str, str]:
    path = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "nas" / "keys.env"
    data: dict[str, str] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def _opencode_auth_key(provider_id: str) -> str | None:
    """Reuse a key already stored by opencode, so adding a provider 'just works'.

    opencode keeps credentials in ~/.local/share/opencode/auth.json as
    {"<provider>": {"type": "api", "key": "..."}}. Read-only; never logged.
    """
    share = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    for path in (share / "opencode" / "auth.json",
                 Path.home() / ".local" / "share" / "opencode" / "auth.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        entry = data.get(provider_id)
        if isinstance(entry, dict):
            for field in ("key", "apiKey", "api_key", "access"):
                if entry.get(field):
                    return str(entry[field])
    return None


def resolve_key(cfg: dict, key: str, env_name: str, opencode_id: str | None = None) -> str | None:
    value = provider_setting(cfg, key, "api_key")
    if value:
        return str(value)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    keys = _keys_file()
    if env_name and keys.get(env_name):
        return keys[env_name]
    if opencode_id:
        return _opencode_auth_key(opencode_id)
    return None


def key_source(cfg: dict, key: str, env_name: str, opencode_id: str | None = None) -> str:
    """A short description of where the key came from, for display."""
    if provider_setting(cfg, key, "api_key"):
        return "NAS config"
    if env_name and os.environ.get(env_name):
        return f"${env_name}"
    if env_name and _keys_file().get(env_name):
        return "keys.env"
    if opencode_id and _opencode_auth_key(opencode_id):
        return "opencode"
    return ""


def _day_start(ts: float | None = None) -> float:
    dt = datetime.fromtimestamp(ts if ts else time.time())
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# Approximate prices, USD per 1M tokens: (input, output, cache_read, cache_write).
# Used only to estimate cost for local logs that carry tokens but no billing.
PRICING = {
    "claude-opus-4": (15.0, 75.0, 1.5, 18.75),
    "claude-3-opus": (15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4": (3.0, 15.0, 0.3, 3.75),
    "claude-3-7-sonnet": (3.0, 15.0, 0.3, 3.75),
    "claude-3-5-sonnet": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4": (1.0, 5.0, 0.1, 1.25),
    "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0),
    "gpt-5": (1.25, 10.0, 0.125, 1.25),
    "gpt-5-codex": (1.25, 10.0, 0.125, 1.25),
    "o3": (2.0, 8.0, 0.5, 2.0),
    "o4-mini": (1.1, 4.4, 0.275, 1.1),
}


def estimate_cost(model: str, inp: float, out: float, cache_read: float, cache_write: float) -> float | None:
    key = (model or "").lower()
    match = next((k for k in PRICING if key.startswith(k)), None)
    if not match:
        return None
    pin, pout, pcr, pcw = PRICING[match]
    return (inp * pin + out * pout + cache_read * pcr + cache_write * pcw) / 1_000_000


# --------------------------------------------------------------------------
# opencode (local database)
# --------------------------------------------------------------------------

def _opencode_db() -> Path | None:
    candidates = [
        Path.home() / ".local" / "share" / "opencode" / "opencode.db",
        Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode" / "opencode.db",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def detect_opencode() -> tuple[str, str]:
    db = _opencode_db()
    if db:
        return "ready", f"database at {db}"
    return "missing", "no opencode database found"


def fetch_opencode(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="opencode", title="opencode", window="today", unit="$",
                    plan="local", source="opencode.db")
    db = _opencode_db()
    if not db:
        snap.error = "no opencode database found"
        snap.recompute_status()
        return snap
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sessions = con.execute("select count(*) as n from session").fetchone()["n"]
        today = _day_start()
        cost_today = cost_7d = cost_30d = cost_total = 0.0
        tok = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        by_day: dict[int, float] = {}
        by_model: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        for row in con.execute("select data from message order by time_created"):
            try:
                j = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if j.get("role") != "assistant":
                continue
            cost = float(j.get("cost") or 0.0)
            t = (j.get("time") or {}).get("created")
            tokens = j.get("tokens") or {}
            cache = tokens.get("cache") or {}
            tok["input"] += tokens.get("input", 0)
            tok["output"] += tokens.get("output", 0)
            tok["reasoning"] += tokens.get("reasoning", 0)
            tok["cache_read"] += cache.get("read", 0)
            tok["cache_write"] += cache.get("write", 0)
            cost_total += cost
            if t:
                day = int(_day_start(t / 1000 if t > 1e12 else t))
                by_day[day] = by_day.get(day, 0.0) + cost
            model = j.get("modelID") or "unknown"
            by_model[model] = by_model.get(model, 0.0) + cost
            provider_id = j.get("providerID") or "unknown"
            by_provider[provider_id] = by_provider.get(provider_id, 0.0) + cost
        con.close()
        for day, value in by_day.items():
            if day >= _day_start():
                cost_today += value
            if day >= _day_start() - 6 * 86400:
                cost_7d += value
            if day >= _day_start() - 29 * 86400:
                cost_30d += value
        # Fall back to the session table if message parsing found nothing.
        if cost_total == 0 and sessions:
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                row = con.execute("select sum(cost) as c from session").fetchone()
                cost_total = cost_30d = cost_7d = cost_today = float(row["c"] or 0.0)
                con.close()
            except sqlite3.Error:
                pass

        cache_total = tok["cache_read"] + tok["input"]
        hit = (tok["cache_read"] / cache_total * 100) if cache_total else 0.0
        upstream = sorted(k for k in by_provider if k != "unknown")
        snap.used = cost_today
        snap.plan = " · ".join(upstream) if upstream else "local"
        snap.tokens = dict(tok)
        snap.sessions = sessions
        snap.summary = f"{sessions} session{'s' if sessions != 1 else ''} · {fmt_tokens(tok['input'] + tok['output'])} tokens"
        snap.details = []
        if upstream:
            snap.details.append(("Used with", ", ".join(upstream)))
        snap.details += [
            ("Cost today", f"${cost_today:,.4f}"),
            ("Cost 7 days", f"${cost_7d:,.4f}"),
            ("Cost 30 days", f"${cost_30d:,.4f}"),
            ("Cost total", f"${cost_total:,.4f}"),
            ("Input", fmt_tokens(tok["input"])),
            ("Output", fmt_tokens(tok["output"])),
            ("Reasoning", fmt_tokens(tok["reasoning"])),
            ("Cache read", fmt_tokens(tok["cache_read"])),
            ("Cache write", fmt_tokens(tok["cache_write"])),
            ("Cache hit", f"{hit:.0f}%"),
        ]
        for provider_id, cost in sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True):
            if provider_id != "unknown":
                snap.details.append((f"  via {provider_id}", f"${cost:,.4f}"))
        if by_model:
            top = sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)[:5]
            for model, cost in top:
                snap.details.append((f"  {model}", f"${cost:,.4f}"))
        series = [v for _, v in sorted(by_day.items())][-24:]
        if series:
            snap.details.append(("Trend", sparkline(series)))
        snap.confidence = "exact"
    except sqlite3.Error as exc:
        snap.error = f"cannot read opencode database: {exc}"
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# DeepSeek (official balance API)
# --------------------------------------------------------------------------

def detect_deepseek() -> tuple[str, str]:
    source = key_source(load_config(), "deepseek", "DEEPSEEK_API_KEY", "deepseek")
    if source:
        return "ready", f"API key found ({source})"
    return "needs-key", "set DEEPSEEK_API_KEY or add it in NAS"


def fetch_deepseek(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="deepseek", title="DeepSeek API", window="prepaid",
                    unit="", plan="DeepSeek", source="api.deepseek.com/user/balance")
    key = resolve_key(cfg, "deepseek", "DEEPSEEK_API_KEY", "deepseek")
    if not key:
        snap.error = "no API key (set DEEPSEEK_API_KEY or add it in NAS)"
        snap.recompute_status()
        return snap
    try:
        data = _http_json("https://api.deepseek.com/user/balance",
                          {"Authorization": f"Bearer {key}", "Accept": "application/json"})
        infos = data.get("balance_infos") or []
        if not infos:
            snap.error = "empty balance response"
            snap.recompute_status()
            return snap
        info = infos[0]
        symbols = {"CNY": "¥", "USD": "$", "EUR": "€"}
        currency = info.get("currency", "USD")
        symbol = symbols.get(currency, currency + " ")
        try:
            total = float(info.get("total_balance", 0))
            granted = float(info.get("granted_balance", 0))
            topped = info.get("topped_up_balance")
        except (TypeError, ValueError):
            total = granted = 0.0
            topped = None
        snap.unit = symbol
        snap.used = None
        snap.remaining = total
        snap.plan = f"DeepSeek API ({currency})"
        snap.summary = "prepaid balance" if data.get("is_available", True) else "balance unavailable"
        snap.details = [
            ("Total balance", f"{symbol}{total:,.2f}"),
            ("Granted", f"{symbol}{granted:,.2f}"),
            ("Topped up", f"{symbol}{float(topped):,.2f}" if topped is not None else "-"),
            ("Available", "yes" if data.get("is_available", True) else "no"),
            ("Key", key_source(cfg, "deepseek", "DEEPSEEK_API_KEY", "deepseek") or "-"),
        ]
        if total < 5:
            snap.status = "warn"
        snap.confidence = "exact"
    except urllib.error.HTTPError as exc:
        snap.error = f"HTTP {exc.code} from DeepSeek"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        snap.error = f"could not reach DeepSeek: {exc}"
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# Claude Code (local transcripts)
# --------------------------------------------------------------------------

def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def detect_claude() -> tuple[str, str]:
    base = _claude_dir() / "projects"
    if base.is_dir() and any(base.rglob("*.jsonl")):
        return "ready", f"transcripts at {base}"
    if base.is_dir():
        return "ready", "no transcripts yet"
    return "missing", "no Claude Code transcripts found"


def fetch_claude(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="claude-code", title="Claude Code", window="today", unit="$",
                    plan="Claude Code", source="~/.claude/projects")
    base = _claude_dir() / "projects"
    if not base.is_dir():
        snap.error = "no Claude Code transcripts found"
        snap.recompute_status()
        return snap
    today = _day_start()
    cost_today = cost_total = 0.0
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    by_day: dict[int, float] = {}
    priced = True
    files = 0
    for path in base.rglob("*.jsonl"):
        files += 1
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if not usage:
                    continue
                model = (obj.get("message") or {}).get("model", "")
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cr = usage.get("cache_read_input_tokens", 0)
                cw = usage.get("cache_creation_input_tokens", 0)
                tok["input"] += inp
                tok["output"] += out
                tok["cache_read"] += cr
                tok["cache_write"] += cw
                cost = estimate_cost(model, inp, out, cr, cw)
                if cost is None:
                    priced = False
                else:
                    stamp = _parse_iso(obj.get("timestamp")) or time.time()
                    day = int(_day_start(stamp))
                    by_day[day] = by_day.get(day, 0.0) + cost
                    cost_total += cost
                    if day >= today:
                        cost_today += cost
    snap.used = cost_today
    snap.confidence = "estimated" if priced else "unknown"
    snap.tokens = dict(tok)
    snap.sessions = files
    cache_total = tok["cache_read"] + tok["input"]
    hit = (tok["cache_read"] / cache_total * 100) if cache_total else 0.0
    snap.summary = f"{files} transcript{'s' if files != 1 else ''} · {fmt_tokens(tok['input'] + tok['output'])} tokens"
    snap.details = [
        ("Cost today", f"${cost_today:,.4f}"),
        ("Cost total", f"${cost_total:,.4f}" + ("" if priced else " (unpriced models)")),
        ("Input", fmt_tokens(tok["input"])),
        ("Output", fmt_tokens(tok["output"])),
        ("Cache read", fmt_tokens(tok["cache_read"])),
        ("Cache write", fmt_tokens(tok["cache_write"])),
        ("Cache hit", f"{hit:.0f}%"),
    ]
    series = [v for _, v in sorted(by_day.items())][-24:]
    if series:
        snap.details.append(("Trend", sparkline(series)))
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# Codex CLI (local sessions)
# --------------------------------------------------------------------------

def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def detect_codex() -> tuple[str, str]:
    base = _codex_dir()
    if base.is_dir() and any(base.rglob("*.jsonl")):
        return "ready", f"sessions at {base}"
    if base.is_dir():
        return "ready", "no sessions yet"
    return "missing", "no Codex sessions found"


def _codex_totals(usage: dict) -> tuple[int, int, int]:
    inp = usage.get("input_tokens", 0) or 0
    cached = usage.get("cached_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    return inp, cached, out


def fetch_codex(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="codex", title="Codex CLI", window="today", unit="$",
                    plan="Codex", source="~/.codex")
    base = _codex_dir()
    if not base.is_dir():
        snap.error = "no Codex sessions found"
        snap.recompute_status()
        return snap
    today = _day_start()
    cost_today = cost_total = 0.0
    tok_in = tok_out = tok_cached = 0
    model = ""
    files = 0
    priced = True
    for path in base.rglob("*.jsonl"):
        files += 1
        # A Codex transcript can repeat cumulative totals, so keep the last one
        # seen per file for the session total.
        last_total: dict | None = None
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if "token" not in line and "model" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") or {}
                if obj.get("type") == "turn_context" and payload.get("model"):
                    model = payload["model"]
                info = payload.get("info") or {}
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    last_total = info["total_token_usage"]
        if not last_total:
            continue
        inp, cached, out = _codex_totals(last_total)
        tok_in += inp
        tok_out += out
        tok_cached += cached
        cost = estimate_cost(model, inp - cached, out, cached, 0)
        if cost is None:
            priced = False
        else:
            cost_total += cost
            mtime = path.stat().st_mtime
            if mtime >= today:
                cost_today += cost
    snap.used = cost_today
    snap.confidence = "estimated" if priced else "unknown"
    snap.tokens = {"input": max(0, tok_in - tok_cached), "cache_read": tok_cached, "output": tok_out}
    snap.sessions = files
    snap.summary = f"{files} session{'s' if files != 1 else ''} · {fmt_tokens(tok_in + tok_out)} tokens"
    if model:
        snap.plan = f"Codex ({model})"
    snap.details = [
        ("Cost today", f"${cost_today:,.4f}"),
        ("Cost total", f"${cost_total:,.4f}" + ("" if priced else " (unpriced models)")),
        ("Input", fmt_tokens(tok_in)),
        ("Cached input", fmt_tokens(tok_cached)),
        ("Output", fmt_tokens(tok_out)),
    ]
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# OpenAI (official organisation costs API)
# --------------------------------------------------------------------------

def detect_openai() -> tuple[str, str]:
    source = key_source(load_config(), "openai", "OPENAI_ADMIN_KEY", "openai")
    if source:
        return "ready", f"admin key found ({source})"
    return "needs-key", "set OPENAI_ADMIN_KEY (Admin key)"


def fetch_openai(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="openai", title="OpenAI API", window="30d", unit="$",
                    plan="OpenAI", source="api.openai.com/v1/organization/costs")
    key = resolve_key(cfg, "openai", "OPENAI_ADMIN_KEY", "openai")
    if not key:
        snap.error = "no Admin API key (set OPENAI_ADMIN_KEY)"
        snap.recompute_status()
        return snap
    start = int(_day_start() - 29 * 86400)
    url = f"https://api.openai.com/v1/organization/costs?start_time={start}&bucket_width=1d&limit=31"
    try:
        data = _http_json(url, {"Authorization": f"Bearer {key}"})
        total = 0.0
        currency = "USD"
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                amount = result.get("amount") or {}
                total += float(amount.get("value", 0) or 0)
                currency = amount.get("currency", currency)
        symbol = {"USD": "$", "EUR": "€"}.get(currency, currency + " ")
        snap.unit = symbol
        snap.used = total
        snap.limit = None
        snap.summary = "spend, last 30 days"
        snap.details = [("30-day spend", f"{symbol}{total:,.2f}"), ("Currency", currency)]
        snap.confidence = "exact"
    except urllib.error.HTTPError as exc:
        snap.error = f"HTTP {exc.code} (needs an Admin key)"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        snap.error = f"could not reach OpenAI: {exc}"
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# Anthropic (official Admin cost report - experimental)
# --------------------------------------------------------------------------

def detect_anthropic() -> tuple[str, str]:
    source = key_source(load_config(), "anthropic", "ANTHROPIC_ADMIN_KEY", "anthropic")
    if source:
        return "ready", f"admin key found ({source})"
    return "needs-key", "set ANTHROPIC_ADMIN_KEY (Admin key)"


def fetch_anthropic(cfg: dict) -> Snapshot:
    snap = Snapshot(provider="anthropic", title="Anthropic API", window="30d", unit="$",
                    plan="Anthropic", source="api.anthropic.com/v1/organizations/cost_report")
    key = resolve_key(cfg, "anthropic", "ANTHROPIC_ADMIN_KEY", "anthropic")
    if not key:
        snap.error = "no Admin API key (set ANTHROPIC_ADMIN_KEY)"
        snap.recompute_status()
        return snap
    start = datetime.fromtimestamp(_day_start() - 29 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.anthropic.com/v1/organizations/cost_report?starting_at={start}&bucket_width=1d"
    try:
        data = _http_json(url, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        total = 0.0
        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                # amounts arrive in cents as strings
                try:
                    total += float(result.get("amount", 0)) / 100.0
                except (TypeError, ValueError):
                    continue
        snap.used = total
        snap.summary = "spend, last 30 days"
        snap.details = [("30-day spend", f"${total:,.2f}")]
        snap.confidence = "exact"
    except urllib.error.HTTPError as exc:
        snap.error = f"HTTP {exc.code} (needs an Admin key)"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        snap.error = f"could not reach Anthropic: {exc}"
    snap.recompute_status()
    return snap


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

class Provider:
    def __init__(self, key, title, description, kind, detect, fetch,
                 key_env="", key_url="", scope="machine"):
        self.key = key
        self.title = title
        self.description = description
        self.kind = kind
        self.detect = detect
        self.fetch = fetch
        self.key_env = key_env
        self.key_url = key_url
        # "machine" = per-host usage (sum across the fleet);
        # "account" = one figure for the whole account (show once).
        self.scope = scope

    def status(self) -> tuple[str, str]:
        try:
            return self.detect()
        except Exception as exc:  # noqa: BLE001 - detection must never crash the UI
            return "missing", str(exc)


PROVIDERS: list[Provider] = [
    Provider("opencode", "opencode", "Token use and cost from the local opencode database",
             "local", detect_opencode, fetch_opencode, scope="machine"),
    Provider("deepseek", "DeepSeek API", "Prepaid balance from api.deepseek.com",
             "api", detect_deepseek, fetch_deepseek, scope="account",
             key_env="DEEPSEEK_API_KEY", key_url="https://platform.deepseek.com/api_keys"),
    Provider("claude-code", "Claude Code", "Usage from your local Claude Code transcripts",
             "local", detect_claude, fetch_claude, scope="machine"),
    Provider("codex", "Codex CLI", "Usage from your local Codex session logs",
             "local", detect_codex, fetch_codex, scope="machine"),
    Provider("openai", "OpenAI API", "30-day organisation spend (Admin key)",
             "api", detect_openai, fetch_openai, scope="account",
             key_env="OPENAI_ADMIN_KEY", key_url="https://platform.openai.com/settings/organization/admin-keys"),
    Provider("anthropic", "Anthropic API", "30-day cost report (Admin key, experimental)",
             "api", detect_anthropic, fetch_anthropic, scope="account",
             key_env="ANTHROPIC_ADMIN_KEY", key_url="https://console.anthropic.com/settings/admin-keys"),
]

PROVIDER_MAP = {p.key: p for p in PROVIDERS}


def get_provider(key: str) -> Provider | None:
    return PROVIDER_MAP.get(key)


def fetch_all(cfg: dict) -> list[Snapshot]:
    snaps: list[Snapshot] = []
    for key in cfg.get("enabled", []):
        provider = PROVIDER_MAP.get(key)
        if provider is None:
            continue
        try:
            snap = provider.fetch(cfg)
        except Exception as exc:  # noqa: BLE001 - one bad provider must not stop the rest
            snap = Snapshot(provider=key, title=provider.title, error=str(exc))
            snap.recompute_status()
        snaps.append(snap)
    return snaps
