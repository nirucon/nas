#!/usr/bin/env bash
# Installer for NAS - Niru Agent Stats.
# Puts the tool in ~/.local/bin so the terminal, the menu and any status-bar
# hook all run the same copy. No root rights, no third-party dependencies:
# Python 3.10+ and its standard library are all that is needed.
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/nas"
APPLICATIONS="${HOME}/.local/share/applications"

if [[ ${EUID} -eq 0 ]]; then
  echo "Run this as your normal user, not with sudo." >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "python3 is missing. Install it first (python3 / python)." >&2
  exit 1
fi

major="$(python3 -c 'import sys; print(sys.version_info.major)')"
minor="$(python3 -c 'import sys; print(sys.version_info.minor if sys.version_info.major == 3 else 0)')"
if [[ ${major} -ne 3 || ${minor} -lt 10 ]]; then
  echo "Python 3.10 or newer is required (found ${major}.${minor})." >&2
  exit 1
fi

mkdir -p "${BIN_DIR}" "${SHARE_DIR}" "${APPLICATIONS}"

for module in "${SOURCE_DIR}"/scripts/*.py; do
  name="$(basename "${module}")"
  if [[ ${name} == nas.py ]]; then
    install -m 0755 "${module}" "${SHARE_DIR}/${name}"
  else
    install -m 0644 "${module}" "${SHARE_DIR}/${name}"
  fi
done

cat > "${BIN_DIR}/nas" <<EOF
#!/usr/bin/env bash
exec python3 "${SHARE_DIR}/nas.py" "\$@"
EOF
chmod 0755 "${BIN_DIR}/nas"

# A desktop entry so NAS shows up in the Super+Shift+p launcher (rofi drun).
# Prefer launching inside st (NIRUPI's terminal) when it is available.
if command -v st >/dev/null; then
  exec_line="st -e nas"
  terminal=false
else
  exec_line="nas"
  terminal=true
fi
cat > "${APPLICATIONS}/nas.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Niru Agent Stats
Comment=Usage and balance for your AI subscriptions
Exec=${exec_line}
Terminal=${terminal}
Categories=System;Monitor;Utility;
Keywords=nas;usage;tokens;cost;agents;deepseek;claude;codex;openai;
EOF
chmod 0644 "${APPLICATIONS}/nas.desktop"

if command -v update-desktop-database >/dev/null; then
  update-desktop-database "${APPLICATIONS}" 2>/dev/null || true
fi

v="$(cat "${SOURCE_DIR}/VERSION" 2>/dev/null || echo '?')"
printf '\n  ✓ NAS %s installed\n\n' "${v}"
printf '    Command:    %s/nas\n' "${BIN_DIR}"
printf '    Modules:    %s\n' "${SHARE_DIR}"
printf '    Menu entry: %s/nas.desktop\n\n' "${APPLICATIONS}"

case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) printf '    Note: %s is not on your PATH.\n' "${BIN_DIR}"
     printf '          Add: export PATH="%s:$PATH"\n\n' "${BIN_DIR}" ;;
esac

printf '    Start with:       nas\n'
printf '    Add providers:    nas add opencode\n'
printf '    Non-interactive:  nas list | nas --json | nas --status\n\n'
