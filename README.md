# NAS - Niru Agent Stats

Early version, made for my own machines. Shows how much of my AI usage is
left, in one place: opencode, Claude Code, Codex, DeepSeek, OpenAI, Anthropic.

```
 NAS   Niru Agent Stats                          Noir  ·  20:01
────────────────────────────────────────────────────────────────
 ╭─ opencode ───────────────────────────────────────── ● ok ─╮
 │ $0.21  today                                              │
 │ 1 session · 191.8K tokens                                 │
 ╰───────────────────────────────────────────────────────────╯

a add  d remove  ↵ details  r refresh  ? help  q quit
```

## Install

```sh
git clone https://github.com/nirucon/nas
cd nas
./install.sh
```

Python 3.10+ and the standard library only. Debian, Arch and Void. Installs to
~/.local/bin/nas and adds a desktop entry for the launcher.

## Use

```sh
nas
```

Keys: `a` add, `d` remove, enter details, `r` refresh, `?` help, `q` quit.

```sh
nas list
nas --json
nas --status
nas add opencode
nas remove codex
```

Only the providers you add are shown. API providers read a key from the
environment, `~/.config/nas/keys.env`, or a key already stored by opencode.

## Providers

`opencode`, `claude-code` and `codex` read local files, no keys needed.
`deepseek`, `openai` and `anthropic` read an official endpoint with a key.

## Fleet

Reads `~/.config/nirupi/fleet.conf` and merges usage over SSH. Per-machine
providers (`opencode`, `claude-code`, `codex`) add up across hosts, account
providers (`deepseek`, `openai`, `anthropic`) are shown once. NAS has to be
installed on each host.

## Theme

Follows the active NIRUPI palette.

```sh
nas --themes
nas --theme Hackerman
```

## Config

`~/.config/nas/config.json`. Only the `enabled` providers are shown.

## License

MIT
