#!/usr/bin/env python3
"""NAS - Niru Agent Stats.

See how much of your AI subscriptions and API credit you have used, in one
place. Local sources need no keys; API sources read a key from the environment,
the NAS config, or ~/.config/nas/keys.env.

Run `nas` for the interface, or `nas --help` for the plain commands.
"""

from __future__ import annotations

import argparse
import json
import sys

import nasfleet
import naslib
import nasproviders


def _enabled(cfg: dict) -> list[str]:
    return [k for k in cfg.get("enabled", []) if k in nasproviders.PROVIDER_MAP]


def _gather(cfg: dict, fleet: bool):
    snaps, _ = nasfleet.collect(cfg, fleet)
    return snaps


def cmd_list(cfg: dict, fleet: bool = False) -> int:
    snaps = _gather(cfg, fleet)
    if not snaps:
        print("No providers enabled. Add one with:  nas add <name>")
        print("Available: " + ", ".join(p.key for p in nasproviders.PROVIDERS))
        return 0
    for snap in snaps:
        head = f"{snap.title:<16}"
        if snap.error:
            print(f"{head} error: {snap.error}")
            continue
        if snap.used is not None and snap.unit == "$":
            value = naslib.fmt_money(snap.used)
        elif snap.remaining is not None:
            value = f"{snap.unit}{snap.remaining:,.2f}"
        elif snap.used is not None:
            value = naslib.fmt_tokens(snap.used)
        else:
            value = "-"
        window = snap.window or ""
        frac = snap.fraction
        pct = f"  {frac * 100:4.1f}%" if frac is not None else ""
        print(f"{head} {value:>12}  {window:<8}{pct}  {snap.summary}")
    return 0


def cmd_status(cfg: dict, fleet: bool = False) -> int:
    """One line, for a status bar."""
    snaps = _gather(cfg, fleet)
    if not snaps:
        print("NAS: none")
        return 0
    bits = []
    for snap in snaps:
        if snap.error:
            bits.append(f"{snap.title}: !")
            continue
        if snap.used is not None and snap.unit == "$":
            bits.append(f"{snap.title}: {naslib.fmt_money(snap.used)}")
        elif snap.remaining is not None:
            bits.append(f"{snap.title}: {snap.unit}{snap.remaining:,.2f} left")
        else:
            bits.append(snap.title)
    print("NAS: " + " · ".join(bits))
    return 0


def cmd_json(cfg: dict, fleet: bool = False) -> int:
    snaps = _gather(cfg, fleet)
    out = []
    for snap in snaps:
        out.append({
            "provider": snap.provider,
            "title": snap.title,
            "plan": snap.plan,
            "window": snap.window,
            "unit": snap.unit,
            "used": snap.used,
            "limit": snap.limit,
            "remaining": snap.remaining,
            "reset_at": snap.reset_at,
            "status": snap.status,
            "confidence": snap.confidence,
            "source": snap.source,
            "error": snap.error,
            "tokens": snap.tokens,
            "sessions": snap.sessions,
            "details": snap.details,
        })
    print(json.dumps(out, indent=2))
    return 0


def cmd_providers(cfg: dict) -> int:
    enabled = set(cfg.get("enabled", []))
    print("Providers (add with: nas add NAME)")
    for provider in nasproviders.PROVIDERS:
        status, detail = provider.status()
        mark = "[x]" if provider.key in enabled else "[ ]"
        print(f"  {mark} {provider.key:<12} {provider.title:<16} {provider.kind:<6} {status:<10} {detail}")
    return 0


def cmd_add(cfg: dict, key: str) -> int:
    if key not in nasproviders.PROVIDER_MAP:
        print(f"Unknown provider '{key}'. Available: " + ", ".join(p.key for p in nasproviders.PROVIDERS))
        return 2
    enabled = list(cfg.get("enabled", []))
    if key not in enabled:
        enabled.append(key)
        cfg["enabled"] = enabled
        naslib.save_config(cfg)
        provider = nasproviders.PROVIDER_MAP[key]
        status, detail = provider.status()
        print(f"Added {provider.title} ({status}: {detail}).")
        if status == "needs-key" and provider.key_url:
            print(f"  Get a key: {provider.key_url}")
            print(f"  Then:  export {provider.key_env}=…")
    else:
        print(f"{key} is already enabled.")
    return 0


def cmd_remove(cfg: dict, key: str) -> int:
    enabled = [k for k in cfg.get("enabled", []) if k != key]
    cfg["enabled"] = enabled
    naslib.save_config(cfg)
    print(f"Removed {key}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nas",
        description="Niru Agent Stats - usage and balance for your AI subscriptions in one place.",
    )
    parser.add_argument("--version", action="version", version=f"NAS {naslib.VERSION}")
    parser.add_argument("--theme", metavar="NAME", help="override the palette (see --themes)")
    parser.add_argument("--truecolor", choices=["auto", "on", "off"], default=None,
                        help="colour depth override")
    parser.add_argument("--refresh", type=int, metavar="SECONDS", help="auto-refresh interval")
    parser.add_argument("--list", action="store_true", help="print a table and exit")
    parser.add_argument("--status", action="store_true", help="print one line (status bar)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--providers", action="store_true", help="list available providers")
    parser.add_argument("--themes", action="store_true", help="list NIRUPI palettes")
    parser.add_argument("--fleet", action="store_true",
                        help="merge usage from the NIRUPI fleet.conf hosts")
    parser.add_argument("--no-fleet", action="store_true", help="ignore the fleet, local only")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="print a table")
    sub.add_parser("status", help="print one line")
    sub.add_parser("json", help="emit JSON")
    sub.add_parser("providers", help="list available providers")
    p_add = sub.add_parser("add", help="enable a provider")
    p_add.add_argument("name")
    p_rm = sub.add_parser("remove", help="disable a provider")
    p_rm.add_argument("name")

    args = parser.parse_args(argv)
    cfg = naslib.load_config()
    if args.refresh:
        cfg["refresh_seconds"] = args.refresh

    fleet_pref = cfg.get("fleet", "auto")

    if args.themes:
        print("\n".join(naslib.list_themes()))
        return 0
    if args.providers or args.cmd == "providers":
        return cmd_providers(cfg)
    if args.cmd == "add":
        return cmd_add(cfg, args.name)
    if args.cmd == "remove":
        return cmd_remove(cfg, args.name)

    # Fleet: on for the interface (auto), off for the one-shot commands unless
    # asked for, so a status bar never waits on SSH.
    cli_fleet = (args.fleet and not args.no_fleet) or (fleet_pref is True and not args.no_fleet)
    if args.list or args.cmd == "list":
        return cmd_list(cfg, cli_fleet)
    if args.status or args.cmd == "status":
        return cmd_status(cfg, cli_fleet)
    if args.json or args.cmd == "json":
        return cmd_json(cfg, cli_fleet)

    # No command: open the interface when we have a terminal, else show a table.
    theme_name = args.theme or cfg.get("theme", "auto")
    truecolor = args.truecolor or cfg.get("truecolor", "auto")
    palette = naslib.load_palette(theme_name, truecolor)
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return cmd_list(cfg, cli_fleet)

    if args.no_fleet:
        use_fleet = False
    elif args.fleet:
        use_fleet = True
    else:
        use_fleet = fleet_pref is not False and bool(nasfleet.fleet_hosts())

    import nastui
    app = nastui.App(cfg, nastui.Theme(palette), use_fleet=use_fleet)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
