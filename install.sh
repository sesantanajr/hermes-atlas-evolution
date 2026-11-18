#!/bin/sh
# Hermes Atlas Evolution: install or update the plugin in every Hermes profile, or in the ones you pick.
#
#   sh install.sh                      # asks which profiles (Enter = all)
#   sh install.sh --all --yes          # every profile, no questions
#   sh install.sh --profiles default,work --install-missing
#   sh install.sh --all --yes --restart
#   sh install.sh --all --install-missing --catalog   # from the Hermes plugin catalog (the reviewed version)
#
# It only runs Hermes' own commands, one profile at a time, and never picks a commit itself:
#   installed            -> hermes --profile <p> plugins update hermes-atlas-evolution
#                           (Hermes decides: the catalog's reviewed pin, or a pull of the source you installed from)
#   pinned to a commit   -> left as it is, with the command that moves it
#   half-installed       -> hermes --profile <p> plugins install <source> --force
#   missing (on request) -> hermes --profile <p> plugins install <source> --enable
# <source> is this repository, or with --catalog the catalog name. A development link is never touched, nor a fork's
# pinned or half-installed copy, and a failed update is reported, never followed by a reinstall. A failure in one
# profile never stops the others. With --restart it checks the gateway's service first (a service pointing at a Python
# that no longer exists cannot start) and only then restarts it. Everything runs from main(), called on the last line.
set -u

PLUGIN="hermes-atlas-evolution"
URL="https://github.com/sesantanajr/hermes-atlas-evolution.git"
OURS="github.com/sesantanajr/hermes-atlas-evolution"
ALL=0; YES=0; MISSING=0; RESTART=0; PROFILES=""; SOURCE="$URL"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
usage() {
  case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
    pt*|PT*)
      say "Uso: sh install.sh [--all | --profiles a,b] [--install-missing] [--catalog] [--yes] [--restart]"
      say "  --all              todos os perfis deste Hermes"
      say "  --profiles a,b     só estes perfis"
      say "                     (sem nenhum dos dois, pergunta no terminal quais; sem terminal, para)"
      say "  --install-missing  instala também onde o plugin ainda não está"
      say "  --catalog          instala pelo catálogo de plugins do Hermes (a versão revisada), não pelo GitHub"
      say "  --yes              sem perguntas"
      say "  --restart          reinicia o gateway no fim (depois de conferir o serviço dele)"
      return ;;
  esac
  say "Usage: sh install.sh [--all | --profiles a,b] [--install-missing] [--catalog] [--yes] [--restart]"
  say "  --all              every profile of this Hermes"
  say "  --profiles a,b     only these profiles"
  say "                     (with neither, it asks on the terminal which ones; without a terminal it stops)"
  say "  --install-missing  also install where the plugin is missing"
  say "  --catalog          install from the Hermes plugin catalog (the reviewed version), not from GitHub"
  say "  --yes              no questions"
  say "  --restart          restart the gateway at the end (after checking its service)"
}

# A terminal to ask on: stdin, or /dev/tty when the script itself comes through a pipe.
has_tty() { [ -t 0 ] || (exec </dev/tty) 2>/dev/null; }
ask() {   # prompt -> the answer in $answer; fails (status 1) at the end of input or on a read error
  printf '%s' "$1"
  answer=""
  if [ -t 0 ]; then read -r answer || return 1; else read -r answer </dev/tty 2>/dev/null || return 1; fi
  return 0
}

home_of() { if [ "$1" = default ]; then printf '%s' "$ROOT"; else printf '%s/profiles/%s' "$ROOT" "$1"; fi; }

is_profile() {   # a folder under profiles/ that Hermes lists: an identity file, no tombstone (profile delete)
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  [ -e "$ROOT/profiles/.deleted/$(basename "$1")" ] && return 1
  # Hermes' identity files without .env and auth.json: a credential file is never touched, not even tested for
  for marker in config.yaml SOUL.md profile.yaml state.db; do
    if [ -f "$1/$marker" ] || [ -L "$1/$marker" ]; then return 0; fi
  done
  return 1
}

# Hermes' install record of this plugin in a profile (plugins/.install-metadata.json: one object per plugin, keyed
# by its name), as one line: the object under the top-level key "hermes-atlas-evolution" only, never a string that
# merely contains the name (another plugin's source URL). Empty when there is none.
record_of() {
  meta="$(home_of "$1")/plugins/.install-metadata.json"
  [ -f "$meta" ] && [ ! -L "$meta" ] || return 0
  tr -d '\r\n' <"$meta" | awk -v key="$PLUGIN" '{
    s = $0; n = length(s); depth = 0; i = 1
    while (i <= n) {
      c = substr(s, i, 1)
      if (c == "\"") {
        j = i + 1; tok = ""
        while (j <= n) {
          d = substr(s, j, 1)
          if (d == "\\") { tok = tok d substr(s, j + 1, 1); j += 2; continue }
          if (d == "\"") break
          tok = tok d; j++
        }
        i = j + 1
        if (depth != 1 || tok != key) continue
        while (i <= n && substr(s, i, 1) ~ /[ \t]/) i++
        if (substr(s, i, 1) != ":") continue
        i++
        while (i <= n && substr(s, i, 1) ~ /[ \t]/) i++
        if (substr(s, i, 1) != "{") continue
        start = i; level = 0; quoted = 0
        while (i <= n) {
          c = substr(s, i, 1)
          if (quoted) { if (c == "\\") { i += 2; continue }; if (c == "\"") quoted = 0 }
          else if (c == "\"") quoted = 1
          else if (c == "{") level++
          else if (c == "}") { level--; if (level == 0) { print substr(s, start, i - start + 1); exit } }
          i++
        }
        exit
      }
      if (c == "{" || c == "[") depth++
      else if (c == "}" || c == "]") depth--
      i++
    }
  }'
}

pinned_of() {   # the commit the profile is pinned to ("pinned" when the record names none), empty when not pinned
  rec="$(record_of "$1")"
  # a catalog install sits on the catalog's reviewed pin, and `plugins update` moves it: never "pinned" here
  printf '%s' "$rec" | grep -Eq '"catalog"[[:space:]]*:[[:space:]]*\{' && return 0
  printf '%s' "$rec" | grep -Eq '"pinned"[[:space:]]*:[[:space:]]*true' || return 0
  sha="$(printf '%s' "$rec" | sed -n 's/.*"revision"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p')"
  printf '%s' "${sha:-pinned}"
}

normalize() {   # a git URL as github.com/owner/name: scheme, user, .git, a trailing / and case dropped
  printf '%s' "$1" | tr 'A-Z' 'a-z' | sed -e 's#^git+##' -e 's#^[a-z]*://##' -e 's#^[^@/]*@\([^:/]*\):#\1/#' \
    -e 's#^[^@/]*@##' -e 's#/*$##' -e 's#\.git$##'
}

origin_of() {   # self | other | "" (nothing says): where this plugin's copy in a profile comes from
  folder="$(home_of "$1")/plugins/$PLUGIN"
  url=""
  if [ -f "$folder/.git/config" ] && [ ! -L "$folder/.git/config" ]; then
    url="$(awk '/^[ \t]*\[/ { inside = ($0 ~ /^[ \t]*\[remote "origin"\]/); next }
      inside && /^[ \t]*url[ \t]*=/ { sub(/^[^=]*=[ \t]*/, ""); sub(/[ \t]*$/, ""); print; exit }' "$folder/.git/config")"
  fi
  if [ -z "$url" ]; then
    url="$(record_of "$1" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  fi
  [ -n "$url" ] || return 0
  case "$(normalize "$url")" in
    "$OURS"|"sesantanajr/hermes-atlas-evolution") echo self ;;
    *) echo other ;;
  esac
}

state_of() {   # linked | broken | pinned | installed | missing
  folder="$(home_of "$1")/plugins/$PLUGIN"
  if [ -L "$folder" ]; then echo linked; return; fi
  if [ -d "$folder" ] && [ ! -f "$folder/plugin.yaml" ]; then echo broken; return; fi
  [ -n "$(pinned_of "$1")" ] && { echo pinned; return; }
  if [ -f "$folder/plugin.yaml" ]; then echo installed; else echo missing; fi
}

run_in() {   # profile, args...: always that profile's own home and name, whatever HERMES_HOME the shell has
  # never with an approval bypass the calling shell may carry (the plugin's own children drop the same ones)
  profile="$1"; shift
  (unset HERMES_PROFILE HERMES_YOLO_MODE HERMES_YOLO HERMES_ACP_AUTO_APPROVE HERMES_ACCEPT_HOOKS HERMES_TUI_NO_CONFIRM \
     CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS
   HERMES_HOME="$(home_of "$profile")"; export HERMES_HOME
   exec "$HERMES" --profile "$profile" "$@" </dev/null)
}


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

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --all) ALL=1 ;;
      --yes|-y) YES=1 ;;
      --install-missing) MISSING=1 ;;
      --restart) RESTART=1 ;;
      --catalog) SOURCE="$PLUGIN" ;;
      --profiles)
        [ $# -ge 2 ] || { err "--profiles needs a value, e.g. --profiles default,work"; exit 2; }
        shift; PROFILES="$1" ;;
      --profiles=*) PROFILES="${1#--profiles=}" ;;
      -h|--help) usage; exit 0 ;;
      *) err "Unknown option: $1"; usage >&2; exit 2 ;;
    esac
    shift
  done

  # ---- dependencies ---------------------------------------------------------------------------------------
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

  # ---- profiles -------------------------------------------------------------------------------------------
  LIST="default"
  if [ -d "$ROOT/profiles" ]; then
    for dir in "$ROOT"/profiles/*; do
      is_profile "$dir" || continue
      name="$(basename "$dir")"
      case "$name" in default|*[!a-z0-9_-]*|-*|_*) continue ;; esac   # Hermes' rule for a profile name
      LIST="$LIST $name"
    done
  fi

  set -f   # the lists below are split on commas and spaces, never expanded as file names
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
    if ! ask "
Profiles to update (Enter = all, e.g. 1,3 or default,work; q = quit): "; then
      say ""; say "Nothing changed."; exit 0   # the end of input is not an answer
    fi
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
    ask "Update $PLUGIN in:$CHOSEN ? [y/N] " || answer=""
    case "$answer" in y|Y|yes|s|S|sim) ;; *) say "Nothing changed."; exit 0 ;; esac
  fi

  # ---- run ------------------------------------------------------------------------------------------------
  OK=0; FAILED=""
  for name in $CHOSEN; do
    state="$(state_of "$name")"
    origin="$(origin_of "$name")"
    say ""; say "== $name ($state)"
    status=0
    case "$state" in
      linked)
        say "   development link: left as it is"; continue ;;
      missing)
        if [ "$MISSING" -ne 1 ]; then say "   not installed: skipped (use --install-missing)"; continue; fi
        run_in "$name" plugins install "$SOURCE" --enable || status=$? ;;
      pinned)
        say "   pinned to $(pinned_of "$name" | cut -c1-12) by Hermes: left as it is. To move it:"
        say "   hermes -p $name plugins install $URL --force --ref <40-character commit>"
        continue ;;
      broken)
        if [ "$origin" = other ]; then
          say "   a copy from a fork or another repository: left as it is"; continue
        fi
        run_in "$name" plugins install "$SOURCE" --force || status=$? ;;
      *)
        # Hermes decides what code comes: the catalog's reviewed pin, or a pull of the source it was installed from
        run_in "$name" plugins update "$PLUGIN" || status=$? ;;
    esac
    if [ "$status" -eq 0 ]; then OK=$((OK + 1)); else FAILED="$FAILED $name"; fi
  done
  set +f

  say ""
  say "Updated: $OK profile(s).${FAILED:+ Failed:$FAILED}"

  # ---- gateway --------------------------------------------------------------------------------------------
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
}

main "$@"
