# shellcheck shell=bash
# Shared helpers for bootstrap.sh and bin/*. Source, don't execute.

INFRA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC2034  # colours are used by scripts that source this file
if [[ -t 1 ]]; then
  c_bold=$'\e[1m'; c_dim=$'\e[2m'; c_red=$'\e[31m'; c_grn=$'\e[32m'; c_yel=$'\e[33m'; c_off=$'\e[0m'
else
  c_bold=""; c_dim=""; c_red=""; c_grn=""; c_yel=""; c_off=""
fi
step() { printf '\n%s==> %s%s\n' "$c_bold" "$*" "$c_off"; }
ok()   { printf '  %s✓%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_yel" "$c_off" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
need() { local b; for b in "$@"; do command -v "$b" >/dev/null || die "'$b' not found in PATH"; done; }

# Lower-case, DNS-safe version of a repo name.
slug() { tr '[:upper:]' '[:lower:]' <<<"$1" | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g'; }

# Load config.env from the repo root (or $INFRA_CONFIG). Required keys are checked.
load_config() {
  local cfg="${INFRA_CONFIG:-$INFRA_ROOT/config.env}"
  [[ -f "$cfg" ]] || die "No config at $cfg — copy config.env.example to config.env and fill it in."
  set -a
  # shellcheck source=/dev/null
  source "$cfg"
  set +a
  local k
  for k in DOMAIN GH_OWNER BAO_ADDR; do
    [[ -n "${!k:-}" ]] || die "$k is not set in $cfg"
  done
  [[ "$DOMAIN" != example.com ]] || die "DOMAIN is still example.com in $cfg"
  INFRA_REPO="${INFRA_REPO:-$GH_OWNER/infra}"
  BAO_VERSION="${BAO_VERSION:-2.7.0}"
  STACK_DIR="${STACK_DIR:-/opt/stack}"
  APPS_DIR="${APPS_DIR:-/opt/apps}"
  TUNNEL_NAME="${TUNNEL_NAME:-$(hostname -s)}"
  SNAPSHOT_KEEP_DAYS="${SNAPSHOT_KEEP_DAYS:-14}"
  export INFRA_REPO BAO_VERSION STACK_DIR APPS_DIR TUNNEL_NAME SNAPSHOT_KEEP_DAYS
}

# Render a template with only the listed variables substituted (leaves other $ alone).
# usage: render <template> <dest> VAR1 VAR2 ...
render() {
  local tmpl="$1" dest="$2"; shift 2
  local vars="" v
  for v in "$@"; do vars+="\$$v "; done
  envsubst "$vars" < "$tmpl" > "$dest"
}
