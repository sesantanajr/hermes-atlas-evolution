#!/bin/sh
# Hermes Atlas Evolution: install or update the plugin in every Hermes profile, or in the ones you pick.
#
#   sh install.sh                      # asks which profiles (Enter = all)
#   sh install.sh --all --yes          # every profile, no questions
#   sh install.sh --profiles default,work --install-missing
#   sh install.sh --all --yes --restart
#
# Remote, without cloning first:
#   curl -fsSL https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/main/install.sh | sh -s -- --all --yes
#
# It only runs Hermes' own commands, one profile at a time:
#   installed            -> hermes -p <p> plugins update hermes-atlas-evolution --force
#   pinned to a commit   -> hermes -p <p> plugins install <url> --force --enable   (clears the pin)
#   missing (on request) -> hermes -p <p> plugins install <url> --enable
# A failure in one profile never stops the others. With --restart it checks the gateway's service first
# (a service pointing at a Python that no longer exists cannot start) and only then restarts it.
set -u

PLUGIN="hermes-atlas-evolution"
URL="https://github.com/sesantanajr/hermes-atlas-evolution.git"
ALL=0; YES=0; MISSING=0; RESTART=0; PROFILES=""

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --all) ALL=1 ;;
    --yes|-y) YES=1 ;;
    --install-missing) MISSING=1 ;;
    --restart) RESTART=1 ;;
    --profiles) shift; PROFILES="${1:-}" ;;
    --profiles=*) PROFILES="${1#--profiles=}" ;;
    -h|--help) sed -n '2,18p' "$0" 2>/dev/null; exit 0 ;;
    *) err "Unknown option: $1"; exit 2 ;;
  esac
  shift
done

# ---- dependencies -----------------------------------------------------------------------------------------
HERMES="$(command -v hermes 2>/dev/null || true)"
for candidate in "$HOME/.local/bin/hermes" /usr/local/bin/hermes /usr/bin/hermes; do
  [ -n "$HERMES" ] && break
  [ -x "$candidate" ] && HERMES="$candidate"
done
if [ -z "$HERMES" ]; then
  err "hermes was not found. Install Hermes Agent first, or add its folder to PATH"
  err "(for example: export PATH=\"\$HOME/.local/bin:\$PATH\")."
  exit 1
fi
command -v git >/dev/null 2>&1 || { err "git is required by 'hermes plugins install'. Install it (apt-get install -y git) and run again."; exit 1; }

ROOT="${HERMES_HOME:-$HOME/.hermes}"
case "$ROOT" in */profiles/*) ROOT="${ROOT%/profiles/*}" ;; esac
[ -d "$ROOT" ] || { err "Hermes home not found at $ROOT (set HERMES_HOME)."; exit 1; }

# ---- profiles ---------------------------------------------------------------------------------------------
home_of() { if [ "$1" = default ]; then printf '%s' "$ROOT"; else printf '%s/profiles/%s' "$ROOT" "$1"; fi; }

LIST="default"
if [ -d "$ROOT/profiles" ]; then
  for dir in "$ROOT"/profiles/*; do
    [ -d "$dir" ] && [ ! -L "$dir" ] && [ -f "$dir/config.yaml" ] || continue
    LIST="$LIST $(basename "$dir")"
  done
fi

state_of() {   # installed | pinned | missing
  folder="$(home_of "$1")/plugins/$PLUGIN"
  [ -f "$folder/plugin.yaml" ] || { echo missing; return; }
  if [ -f "$folder/.install-metadata.json" ] && grep -Eq '"(pinned|pin|ref|pinned_ref)"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"' "$folder/.install-metadata.json"; then
    echo pinned
  else
    echo installed
  fi
}

if [ "$ALL" -eq 1 ]; then
  CHOSEN="$LIST"
elif [ -n "$PROFILES" ]; then
  CHOSEN=""
  for name in $(printf '%s' "$PROFILES" | tr ',' ' '); do
    case " $LIST " in *" $name "*) CHOSEN="$CHOSEN $name" ;; *) err "Unknown profile: $name (known: $LIST)"; exit 2 ;; esac
  done
elif [ -t 0 ] || [ -r /dev/tty ]; then
  say ""; i=0
  for name in $LIST; do i=$((i + 1)); printf '  %2d. %-24s %s\n' "$i" "$name" "$(state_of "$name")"; done
  printf '\nProfiles to update (Enter = all, e.g. 1,3 or default,work; q = quit): '
  read -r answer </dev/tty || answer=q
  case "$answer" in q|Q) say "Nothing changed."; exit 0 ;; esac
  if [ -z "$answer" ]; then
    CHOSEN="$LIST"
  else
    CHOSEN=""
    for part in $(printf '%s' "$answer" | tr ',' ' '); do
      i=0; hit=""
      for name in $LIST; do
        i=$((i + 1))
        if [ "$part" = "$i" ] || [ "$part" = "$name" ]; then hit="$name"; fi
      done
      [ -n "$hit" ] || { err "Unknown profile: $part"; exit 2; }
      CHOSEN="$CHOSEN $hit"
    done
  fi
  YES=1
else
  err "Choose the profiles: --all or --profiles default,work"
  exit 2
fi

if [ "$YES" -ne 1 ]; then
  printf 'Update %s in:%s ? [y/N] ' "$PLUGIN" "$CHOSEN"
  read -r answer </dev/tty || answer=n
  case "$answer" in y|Y|yes|s|S|sim) ;; *) say "Nothing changed."; exit 0 ;; esac
fi

# ---- run --------------------------------------------------------------------------------------------------
run_in() {   # profile, args...
  profile="$1"; shift
  if [ "$profile" = default ]; then "$HERMES" "$@" </dev/null; else "$HERMES" -p "$profile" "$@" </dev/null; fi
}

OK=0; FAILED=""
for name in $CHOSEN; do
  state="$(state_of "$name")"
  say ""; say "== $name ($state)"
  case "$state" in
    missing)
      if [ "$MISSING" -ne 1 ]; then say "   not installed: skipped (use --install-missing)"; continue; fi
      run_in "$name" plugins install "$URL" --enable ;;
    pinned)
      run_in "$name" plugins install "$URL" --force --enable ;;
    *)
      run_in "$name" plugins update "$PLUGIN" --force || run_in "$name" plugins install "$URL" --force --enable ;;
  esac
  if [ $? -eq 0 ]; then OK=$((OK + 1)); else FAILED="$FAILED $name"; fi
done

say ""
say "Updated: $OK profile(s).${FAILED:+ Failed:$FAILED}"

# ---- gateway ----------------------------------------------------------------------------------------------
gateway_exec_ok() {
  command -v systemctl >/dev/null 2>&1 || return 0
  [ -n "${XDG_RUNTIME_DIR:-}" ] || export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  exe="$(systemctl --user show hermes-gateway -p ExecStart --value 2>/dev/null | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -n1)"
  [ -z "$exe" ] && return 0
  if [ ! -x "$exe" ]; then
    err ""
    err "The gateway service points at an interpreter that no longer exists:"
    err "   $exe"
    err "It would fail with status=203/EXEC. Find the line with:"
    err "   grep -rn ExecStart ~/.config/systemd/user/hermes-gateway.service ~/.config/systemd/user/hermes-gateway.service.d/"
    err "fix the path (or reinstall the service with Hermes), then:"
    err "   systemctl --user daemon-reload && systemctl --user reset-failed hermes-gateway && systemctl --user start hermes-gateway"
    return 1
  fi
  return 0
}

if [ "$OK" -gt 0 ]; then
  if [ "$RESTART" -eq 1 ]; then
    if gateway_exec_ok; then "$HERMES" gateway restart; else exit 1; fi
  else
    gateway_exec_ok || true
    say "Restart the gateway once to load the new code: hermes gateway restart"
  fi
fi
[ -z "$FAILED" ]
