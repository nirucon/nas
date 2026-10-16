"""Fleet aggregation for NAS.

Reads the same host list as `nirupi fleet` (~/.config/nirupi/fleet.conf, or a
NAS-local one) and asks each host for `nas --json --no-fleet` over SSH. Then:

* machine-scoped providers (opencode, Claude Code, Codex) are summed across
  every reachable host, with a per-host breakdown;
* account-scoped providers (DeepSeek, OpenAI, Anthropic) are the same for the
  whole account, so they are shown once.

Everything is read-only and non-interactive: SSH runs with BatchMode and a
short connect timeout, and a host that is down or has no NAS installed is
simply reported and skipped.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from naslib import Snapshot, fmt_tokens

REMOTE_CMD = 'export PATH="$HOME/.local/bin:$PATH"; exec nas --json --no-fleet 2>/dev/null'

# Token counters that add up across hosts.
TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write")


def local_hostname() -> str:
    try:
        name = Path("/etc/hostname").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return socket.gethostname() or "local"


def fleet_hosts() -> list[str]:
    """Hosts from NAS's own fleet.conf, else NIRUPI's. One per line."""
    candidates = [
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "nas" / "fleet.conf",
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "nirupi" / "fleet.conf",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        hosts = []
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            host = line.split("#", 1)[0].strip()
            if host:
                hosts.append(host)
        if hosts:
            return hosts
    return []


def fetch_remote(host: str, timeout: int = 8) -> tuple[str, list[Snapshot] | None, str]:
    """Return (host, snapshots, error). snapshots is None when unreachable."""
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        host,
        REMOTE_CMD,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return host, None, "timed out"
    except OSError as exc:
        return host, None, f"ssh failed: {exc}"
    if proc.returncode != 0 and not proc.stdout.strip():
        return host, None, "unreachable / NAS not installed"
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return host, None, "unreachable / NAS not installed"
    if not isinstance(data, list):
        return host, None, "bad response"
    return host, [Snapshot.from_dict(item) for item in data], ""


def gather_remote(cfg: dict) -> tuple[dict[str, list[Snapshot]], list[tuple[str, str]]]:
    """Fetch every fleet host in parallel. Returns (host -> snapshots, errors)."""
    hosts = fleet_hosts()
    remote: dict[str, list[Snapshot]] = {}
    errors: list[tuple[str, str]] = []
    if not hosts:
        return remote, errors
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
        for host, snaps, error in pool.map(fetch_remote, hosts):
            if snaps is None:
                errors.append((host, error))
            else:
                remote[host] = snaps
    return remote, errors


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def _value_text(value: float, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "$":
        return f"${value:,.4f}"
    return fmt_tokens(value)


def _merge_machine(items: list[tuple[str, Snapshot]]) -> Snapshot:
    """Sum a per-host provider across every reachable host."""
    local = local_hostname()
    base = next((s for host, s in items if host == local), None)
    if base is None:
        base = next((s for _, s in items if not s.error), items[0][1])

    used = 0.0
    limit = 0.0
    remaining = 0.0
    has_used = has_limit = has_remaining = False
    tokens: dict[str, int] = {}
    sessions = 0
    hosts_ok: list[str] = []
    host_values: list[tuple[str, float | None]] = []
    errors: list[str] = []

    for host, snap in sorted(items, key=lambda kv: (kv[0] != local, kv[0])):
        if snap.error:
            errors.append(f"{host}: {snap.error}")
            continue
        hosts_ok.append(host)
        if snap.used is not None:
            used += snap.used
            has_used = True
        if snap.limit is not None:
            limit += snap.limit
            has_limit = True
        if snap.remaining is not None:
            remaining += snap.remaining
            has_remaining = True
        for key in TOKEN_KEYS:
            tokens[key] = tokens.get(key, 0) + int((snap.tokens or {}).get(key, 0) or 0)
        sessions += snap.sessions
        host_values.append((host, snap.used))

    out = Snapshot(provider=base.provider, title=base.title, window=base.window,
                   unit=base.unit, plan=base.plan, source=base.source,
                   confidence=base.confidence)
    out.used = round(used, 6) if has_used else None
    out.limit = limit if has_limit and limit > 0 else None
    out.remaining = round(remaining, 6) if has_remaining else None
    out.tokens = tokens
    out.sessions = sessions
    out.summary = (f"{sessions} session{'s' if sessions != 1 else ''} · "
                   f"{fmt_tokens(tokens.get('input', 0) + tokens.get('output', 0))} tokens · "
                   f"{len(hosts_ok)} host{'s' if len(hosts_ok) != 1 else ''}")

    details: list[tuple[str, str]] = []
    if len(hosts_ok) > 1:
        details.append(("Hosts", ", ".join(hosts_ok)))
    for host, value in host_values:
        label = "local" if host == local else host
        details.append((f"  {label}", _value_text(value, out.unit)))
    for key, label in (("input", "Input"), ("output", "Output"), ("reasoning", "Reasoning"),
                       ("cache_read", "Cache read"), ("cache_write", "Cache write")):
        if tokens.get(key):
            details.append((label, fmt_tokens(tokens[key])))
    cache_total = tokens.get("cache_read", 0) + tokens.get("input", 0)
    if cache_total:
        details.append(("Cache hit", f"{tokens.get('cache_read', 0) / cache_total * 100:.0f}%"))
    for err in errors:
        details.append(("host error", err))
    out.details = details
    out.recompute_status()
    return out


def _merge_account(items: list[tuple[str, Snapshot]]) -> Snapshot:
    """Account-level figure: show one, preferring a good local answer."""
    local = local_hostname()
    ordered = sorted(items, key=lambda kv: (kv[0] != local, kv[0]))
    chosen = next((s for _, s in ordered if not s.error), ordered[0][1])
    if chosen.error:
        return chosen
    hosts = [h for h, s in items if not s.error]
    if len(hosts) > 1:
        chosen.details = list(chosen.details) + [("Reported by", ", ".join(hosts))]
    return chosen


def merge(cfg: dict, local: list[Snapshot], remote: dict[str, list[Snapshot]]) -> list[Snapshot]:
    import nasproviders  # local import to avoid a cycle at module load

    local_name = local_hostname()
    grouped: dict[str, list[tuple[str, Snapshot]]] = {}
    for snap in local:
        grouped.setdefault(snap.provider, []).append((local_name, snap))
    for host, snaps in remote.items():
        for snap in snaps:
            grouped.setdefault(snap.provider, []).append((host, snap))

    out: list[Snapshot] = []
    for provider_key, items in grouped.items():
        provider = nasproviders.PROVIDER_MAP.get(provider_key)
        scope = getattr(provider, "scope", "machine")
        out.append(_merge_account(items) if scope == "account" else _merge_machine(items))

    order = {key: index for index, key in enumerate(cfg.get("enabled", []))}
    out.sort(key=lambda snap: order.get(snap.provider, 999))
    return out


def collect(cfg: dict, fleet: bool, progress=None) -> tuple[list[Snapshot], list[tuple[str, str]]]:
    """Full gather: local first (fast), then the fleet (merged)."""
    import nasproviders

    local = nasproviders.fetch_all(cfg)
    if progress:
        progress(local, [])
    if not fleet or not fleet_hosts():
        return local, []
    remote, errors = gather_remote(cfg)
    merged = merge(cfg, local, remote)
    if progress:
        progress(merged, errors)
    return merged, errors
