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
#   installed            -> hermes --profile <p> plugins update hermes-atlas-evolution --force
#   pinned to a commit   -> hermes --profile <p> plugins install <url> --force --ref <latest commit>
#                           (Hermes keeps a pin on a plain reinstall; --ref moves it to the latest commit)
#   half-installed       -> hermes --profile <p> plugins install <url> --force
#   missing (on request) -> hermes --profile <p> plugins install <url> --enable
# A development link is never touched. A failure in one profile never stops the others. With --restart it checks
# the gateway's service first (a service pointing at a Python that no longer exists cannot start) and only then
# restarts it.
set -u

PLUGIN="hermes-atlas-evolution"
URL="https://github.com/sesantanajr/hermes-atlas-evolution.git"
ALL=0; YES=0; MISSING=0; RESTART=0; PROFILES=""

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
usage() {
  say "Usage: sh install.sh [--all | --profiles a,b] [--install-missing] [--yes] [--restart]"
  say "  --all              every profile of this Hermes"
  say "  --profiles a,b     only these profiles (default = the root profile)"
  say "  --install-missing  also install where the plugin is missing"
  say "  --yes              no questions"
  say "  --restart          restart the gateway at the end (after checking its service)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --all) ALL=1 ;;
    --yes|-y) YES=1 ;;
    --install-missing) MISSING=1 ;;
    --restart) RESTART=1 ;;
    --profiles)
      [ $# -ge 2 ] || { err "--profiles needs a value, e.g. --profiles default,work"; exit 2; }
      shift; PROFILES="$1" ;;
    --profiles=*) PROFILES="${1#--profiles=}" ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
  shift
done

# A terminal to ask on: stdin, or /dev/tty when the script itself comes through a pipe (curl ... | sh).
has_tty() { [ -t 0 ] || (exec </dev/tty) 2>/dev/null; }
ask() {   # prompt -> the answer in $answer ("" when there is no terminal)
  printf '%s' "$1"
  if [ -t 0 ]; then read -r answer || answer=""; else read -r answer </dev/tty 2>/dev/null || answer=""; fi
}

# ---- dependencies -----------------------------------------------------------------------------------------
HERMES="$(command -v hermes 2>/dev/null || true)"
for candidate in "${HOME:-/nonexistent}/.local/bin/hermes" /usr/local/bin/hermes /usr/bin/hermes; do
  [ -n "$HERMES" ] && break
  [ -x "$candidate" ] && HERMES="$candidate"
done
if [ -z "$HERMES" ]; then
  err "hermes was not found. Install Hermes Agent first, or add its folder to PATH"
  err "(for example: export PATH=\"\$HOME/.local/bin:\$PATH\")."
  exit 1
fi
command -v git >/dev/null 2>&1 || { err "git is required by 'hermes plugins install'. Install it (apt-get install -y git) and run again."; exit 1; }

ROOT="${HERMES_HOME:-${HOME:-}/.hermes}"
ROOT="${ROOT%/}"
if [ "$(basename "$(dirname "$ROOT")")" = profiles ]; then ROOT="$(dirname "$(dirname "$ROOT")")"; fi
[ -d "$ROOT" ] || { err "Hermes home not found at $ROOT (set HERMES_HOME)."; exit 1; }

# ---- profiles ---------------------------------------------------------------------------------------------
home_of() { if [ "$1" = default ]; then printf '%s' "$ROOT"; else printf '%s/profiles/%s' "$ROOT" "$1"; fi; }

LIST="default"
if [ -d "$ROOT/profiles" ]; then
  for dir in "$ROOT"/profiles/*; do
    [ -d "$dir" ] && [ ! -L "$dir" ] && [ -f "$dir/config.yaml" ] || continue
    name="$(basename "$dir")"
    case "$name" in *[!a-z0-9_-]*|-*|_*) continue ;; esac   # Hermes' rule for a profile name
    LIST="$LIST $name"
  done
fi

# Hermes' install record of this plugin in a profile, on one line (plugins/.install-metadata.json, one object per
# plugin). Empty when there is none.
record_of() {
  meta="$(home_of "$1")/plugins/.install-metadata.json"
  [ -f "$meta" ] || return 0
  tr -d '\r\n' <"$meta" | awk -v key="\"$PLUGIN\"" '{
    at = index($0, key); if (!at) exit
    rest = substr($0, at + length(key)); start = index(rest, "{"); if (!start) exit
    depth = 0
    for (i = start; i <= length(rest); i++) {
      c = substr(rest, i, 1)
      if (c == "{") depth++
      else if (c == "}") { depth--; if (depth == 0) { print substr(rest, start, i - start + 1); exit } }
    }
  }'
}

pinned_of() {   # the commit the profile is pinned to ("pinned" when the record names none), empty when not pinned
  rec="$(record_of "$1")"
  printf '%s' "$rec" | grep -Eq '"pinned"[[:space:]]*:[[:space:]]*true' || return 0
  sha="$(printf '%s' "$rec" | sed -n 's/.*"revision"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p')"
  printf '%s' "${sha:-pinned}"
}

state_of() {   # linked | broken | pinned | installed | missing
  folder="$(home_of "$1")/plugins/$PLUGIN"
  if [ -L "$folder" ]; then echo linked; return; fi
  if [ -d "$folder" ] && [ ! -f "$folder/plugin.yaml" ]; then echo broken; return; fi
  [ -n "$(pinned_of "$1")" ] && { echo pinned; return; }
  if [ -f "$folder/plugin.yaml" ]; then echo installed; else echo missing; fi
}

if [ "$ALL" -eq 1 ]; then
  CHOSEN="$LIST"
elif [ -n "$PROFILES" ]; then
  CHOSEN=""
  for name in $(printf '%s' "$PROFILES" | tr ',' ' '); do
    case " $LIST " in *" $name "*) ;; *) err "Unknown profile: $name (known: $LIST)"; exit 2 ;; esac
    case " $CHOSEN " in *" $name "*) ;; *) CHOSEN="$CHOSEN $name" ;; esac
  done
elif has_tty; then
  say ""; i=0
  for name in $LIST; do i=$((i + 1)); printf '  %2d. %-24s %s\n' "$i" "$name" "$(state_of "$name")"; done
  ask "
Profiles to update (Enter = all, e.g. 1,3 or default,work; q = quit): "
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
      case " $CHOSEN " in *" $hit "*) ;; *) CHOSEN="$CHOSEN $hit" ;; esac
    done
  fi
  YES=1
else
  err "Choose the profiles: --all or --profiles default,work (no terminal to ask on)."
  exit 2
fi

if [ "$YES" -ne 1 ]; then
  has_tty || { err "Add --yes to run without a terminal."; exit 2; }
  ask "Update $PLUGIN in:$CHOSEN ? [y/N] "
  case "$answer" in y|Y|yes|s|S|sim) ;; *) say "Nothing changed."; exit 0 ;; esac
fi

# ---- run --------------------------------------------------------------------------------------------------
run_in() {   # profile, args...: always that profile's own home and name, whatever HERMES_HOME the shell has
  profile="$1"; shift
  (unset HERMES_PROFILE; HERMES_HOME="$(home_of "$profile")"; export HERMES_HOME
   exec "$HERMES" --profile "$profile" "$@" </dev/null)
}

LATEST=""
latest() {   # the repository's latest commit, read once
  if [ -z "$LATEST" ]; then
    LATEST="$(GIT_TERMINAL_PROMPT=0 git ls-remote "$URL" HEAD 2>/dev/null | awk '$2 == "HEAD" && $1 ~ /^[0-9a-f]+$/ && length($1) == 40 { print $1; exit }')"
  fi
  printf '%s' "$LATEST"
}

move_pin() {   # profile: move a pinned install to the latest commit (Hermes' own way: --force --ref <sha>)
  target="$(latest)"
  if [ -z "$target" ]; then
    err "   pinned, and the repository's latest commit could not be read (network or git): left as it is"
    return 1
  fi
  if [ "$(pinned_of "$1")" = "$target" ] && [ -f "$(home_of "$1")/plugins/$PLUGIN/plugin.yaml" ]; then
    say "   already on the latest commit ($(printf '%s' "$target" | cut -c1-8))"
    return 0
  fi
  run_in "$1" plugins install "$URL" --force --ref "$target"
}

OK=0; FAILED=""
for name in $CHOSEN; do
  state="$(state_of "$name")"
  say ""; say "== $name ($state)"
  status=0
  case "$state" in
    linked)
      say "   development link: left as it is"; continue ;;
    missing)
      if [ "$MISSING" -ne 1 ]; then say "   not installed: skipped (use --install-missing)"; continue; fi
      if [ -n "$(pinned_of "$name")" ] && [ -n "$(latest)" ]; then
        run_in "$name" plugins install "$URL" --enable --ref "$(latest)" || status=$?
      else
        run_in "$name" plugins install "$URL" --enable || status=$?
      fi ;;
    pinned)
      move_pin "$name" || status=$? ;;
    broken)
      if [ -n "$(pinned_of "$name")" ]; then move_pin "$name" || status=$?
      else run_in "$name" plugins install "$URL" --force || status=$?; fi ;;
    *)
      if ! run_in "$name" plugins update "$PLUGIN" --force; then
        # a pin the record did not show, an older Hermes, or a copy that is not a git checkout
        if [ -n "$(pinned_of "$name")" ]; then move_pin "$name" || status=$?
        else run_in "$name" plugins install "$URL" --force || status=$?; fi
      fi ;;
  esac
  if [ "$status" -eq 0 ]; then OK=$((OK + 1)); else FAILED="$FAILED $name"; fi
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
    err "It would fail with status=203/EXEC. Repair it with:"
    err "   hermes evolution care fix-gateway"
    err "or find the line with:"
    err "   grep -rn ExecStart ~/.config/systemd/user/hermes-gateway.service ~/.config/systemd/user/hermes-gateway.service.d/"
    err "fix the path (or reinstall the service with Hermes), then:"
    err "   systemctl --user daemon-reload && systemctl --user reset-failed hermes-gateway && systemctl --user start hermes-gateway"
    return 1
  fi
  return 0
}

if [ "$OK" -gt 0 ]; then
  if [ "$RESTART" -eq 1 ]; then
    if gateway_exec_ok; then run_in default gateway restart || { err "The gateway did not restart."; exit 1; }
    else exit 1; fi
  else
    gateway_exec_ok || true
    say "Restart the gateway once to load the new code: hermes gateway restart"
  fi
fi
[ -z "$FAILED" ]
