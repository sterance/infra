#!/usr/bin/env bash
# bootstrap.sh — set up (or rebuild) a server for the infra stack. Idempotent: re-run any time.
#
# Roles (comma-separated, any combination):
#   bao      OpenBao with static-key auto-unseal, served at bao.<DOMAIN> through a Cloudflare Tunnel,
#            plus a nightly snapshot cron job.
#   apps     Caddy + an app area for backends deployed by the deploy-backend workflow.
#            --mode tunnel  apps reached through this machine's Cloudflare Tunnel (no open ports;
#                           deploys run on a self-hosted runner here)  [default]
#            --mode public  apps on public :443 with the Cloudflare Origin cert (deploys over SSH)
#   runner   Prepare a user for a self-hosted GitHub Actions runner (needed for apps in tunnel mode).
#
# Usage (as root, from a clone of this repo with config.env filled in):
#   ./bootstrap.sh --role bao,apps,runner                      # home server, fresh
#   ./bootstrap.sh --role bao,apps,runner --restore bao-2026-10-19.snap   # re-image / move
#   ./bootstrap.sh --role apps --mode public                   # e.g. an Oracle VPS
#   ./bootstrap.sh --role runner --runner-url https://github.com/my-org --runner-token XXX --runner-label home
#
# Options
#   --role LIST           bao, apps, runner (required)
#   --mode tunnel|public  apps mode (default tunnel)
#   --restore FILE        bao: restore this Raft snapshot instead of initialising a new OpenBao
#   --runner-url URL      runner: also register a runner (org URL, or a repo URL on personal accounts)
#   --runner-token TOKEN  runner: registration token (Settings → Actions → Runners → New)
#   --runner-label NAME   runner: label = the target name used in DEPLOY_TARGET (default: TUNNEL_NAME)
#   -h, --help

set -euo pipefail
# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"
TPL="$INFRA_ROOT/stack/templates"

ROLES="" ; MODE=tunnel ; RESTORE="" ; RUNNER_URL="" ; RUNNER_TOKEN="" ; RUNNER_LABEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role|--roles) ROLES="$2"; shift ;;
    --mode) MODE="$2"; shift ;;
    --restore) RESTORE="$2"; shift ;;
    --runner-url) RUNNER_URL="$2"; shift ;;
    --runner-token) RUNNER_TOKEN="$2"; shift ;;
    --runner-label) RUNNER_LABEL="$2"; shift ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)" ;;
  esac
  shift
done

has_role() { [[ ",$ROLES," == *",$1,"* ]]; }
[[ -n "$ROLES" ]] || die "--role is required (bao, apps, runner)"
for r in ${ROLES//,/ }; do [[ "$r" =~ ^(bao|apps|runner)$ ]] || die "Unknown role: $r"; done
[[ "$MODE" == tunnel || "$MODE" == public ]] || die "--mode must be tunnel or public"
[[ -z "$RESTORE" ]] || has_role bao || die "--restore only applies to the bao role"
[[ -z "$RESTORE" || -f "$RESTORE" ]] || die "Snapshot not found: $RESTORE"
[[ $EUID -eq 0 ]] || die "Run as root (sudo ./bootstrap.sh …)"

load_config
RUNNER_LABEL="${RUNNER_LABEL:-$TUNNEL_NAME}"
NEED_TUNNEL=0
if has_role bao || { has_role apps && [[ "$MODE" == tunnel ]]; }; then NEED_TUNNEL=1; fi
CF_IMAGE=cloudflare/cloudflared:latest
BAO_IMAGE="quay.io/openbao/openbao:$BAO_VERSION"

cat <<EOF
${c_bold}Infra bootstrap${c_off}
  Host        $(hostname -s)
  Roles       $ROLES$(has_role apps && echo " (apps mode: $MODE)")
  Domain      $DOMAIN
  Stack dir   $STACK_DIR
  OpenBao     $BAO_ADDR$([[ -n "$RESTORE" ]] && echo "  (restoring $RESTORE)")
EOF

# ---------------------------------------------------------------- packages
step "Packages"
export DEBIAN_FRONTEND=noninteractive
MISSING=()
for p in jq curl gettext-base cron ca-certificates; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if (( ${#MISSING[@]} )); then apt-get update -qq && apt-get install -y -qq "${MISSING[@]}" >/dev/null; fi
ok "jq, curl, envsubst, cron"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
COMPOSE_V=$(docker compose version --short 2>/dev/null || echo 0)
printf '%s\n%s\n' 2.20.0 "${COMPOSE_V#v}" | sort -V -C || die "Docker Compose $COMPOSE_V is too old (need 2.20+ for 'include')"
ok "Docker $(docker version --format '{{.Server.Version}}'), Compose $COMPOSE_V"

# ---------------------------------------------------------------- layout
step "Layout"
install -d -m 755 "$STACK_DIR" "$STACK_DIR/compose"
docker network inspect edge >/dev/null 2>&1 || docker network create edge >/dev/null
ok "$STACK_DIR and docker network 'edge'"
INCLUDES=()

# ---------------------------------------------------------------- role: bao
if has_role bao; then
  step "OpenBao"
  B="$STACK_DIR/openbao"
  install -d -m 755 "$B/config" "$B/file" "$B/snapshots"
  install -d -m 750 "$B/secrets"
  docker pull -q "$BAO_IMAGE" >/dev/null
  BAO_UID=$(docker run --rm --entrypoint id "$BAO_IMAGE" -u openbao)
  BAO_GID=$(docker run --rm --entrypoint id "$BAO_IMAGE" -g openbao)
  chown -R "$BAO_UID:$BAO_GID" "$B/file" "$B/snapshots"
  chown "root:$BAO_GID" "$B/secrets"

  KEY="$B/secrets/unseal.key"
  if [[ ! -s "$KEY" ]]; then
    if [[ -n "$RESTORE" ]]; then
      die "Restoring needs the ORIGINAL unseal key. From your offline kit:
       base64 -d > $KEY   (paste the base64, then Ctrl-D), and re-run."
    fi
    if [[ -n "$(ls -A "$B/file" 2>/dev/null)" ]]; then
      die "$B/file has data but no unseal key — refusing to generate a new key over existing data."
    fi
    openssl rand -out "$KEY" 32
    NEW_KEY=1
    ok "generated a new unseal key"
  fi
  [[ "$(stat -c %s "$KEY")" == 32 ]] || die "$KEY must be exactly 32 bytes (got $(stat -c %s "$KEY"))"
  chown "root:$BAO_GID" "$KEY"; chmod 0440 "$KEY"
  KEY_ID=$(sha256sum "$KEY" | cut -c1-12)
  BAO_NODE_ID=$(hostname -s)
  export KEY_ID BAO_NODE_ID
  render "$TPL/openbao.hcl" "$B/config/config.hcl" BAO_ADDR BAO_NODE_ID KEY_ID
  render "$TPL/compose.openbao.yml" "$STACK_DIR/compose/openbao.yml" BAO_VERSION STACK_DIR
  INCLUDES+=(compose/openbao.yml)
  ok "config rendered (seal key id $KEY_ID)"

  render "$TPL/bao-snapshot.sh" "$STACK_DIR/bao-snapshot.sh" STACK_DIR SNAPSHOT_KEEP_DAYS
  chmod 700 "$STACK_DIR/bao-snapshot.sh"
  echo "30 2 * * * root $STACK_DIR/bao-snapshot.sh >> /var/log/bao-snapshot.log 2>&1" > /etc/cron.d/bao-snapshot
  ok "nightly snapshot at 02:30 → $B/snapshots"
fi

# ---------------------------------------------------------------- role: apps
if has_role apps; then
  step "Apps ($MODE mode)"
  install -d -m 755 "$STACK_DIR/caddy" "$STACK_DIR/caddy/sites" "$APPS_DIR"
  [[ -e "$STACK_DIR/caddy/sites/_empty.caddy" ]] || touch "$STACK_DIR/caddy/sites/_empty.caddy"
  cp "$TPL/Caddyfile" "$STACK_DIR/caddy/Caddyfile"
  if [[ "$MODE" == tunnel ]]; then
    render "$TPL/compose.caddy-tunnel.yml" "$STACK_DIR/compose/caddy.yml" STACK_DIR
  else
    install -d -m 750 "$STACK_DIR/caddy/certs"
    render "$TPL/compose.caddy-public.yml" "$STACK_DIR/compose/caddy.yml" STACK_DIR
    # Deploy user used by GitHub Actions over SSH (certificate auth only)
    id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker deploy
    chown deploy:deploy "$APPS_DIR" "$STACK_DIR/caddy/sites"
    ok "user 'deploy' (docker group) owns $APPS_DIR and caddy/sites"
    # Oracle Cloud Ubuntu images reject everything but SSH in iptables
    if iptables -S INPUT 2>/dev/null | grep -q -- '-j REJECT' \
       && ! iptables -C INPUT -p tcp --dport 443 -m state --state NEW -j ACCEPT 2>/dev/null; then
      iptables -I INPUT 6 -p tcp --dport 443 -m state --state NEW -j ACCEPT
      command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
      ok "opened tcp/443 in iptables (also allow it in your cloud firewall, ideally Cloudflare IPs only)"
    fi
    [[ -s "$STACK_DIR/caddy/certs/origin.pem" ]] \
      || warn "no Origin certificate yet — run bin/register-target from your laptop to install it"
  fi
  INCLUDES+=(compose/caddy.yml)
  ok "Caddy configured; apps live in $APPS_DIR"
fi

# ---------------------------------------------------------------- tunnel
if (( NEED_TUNNEL )); then
  step "Cloudflare Tunnel ($TUNNEL_NAME)"
  C="$STACK_DIR/cloudflared"
  install -d -m 750 "$C"; chown 65532:65532 "$C"
  CF=(docker run --rm -v "$C:/home/nonroot/.cloudflared" "$CF_IMAGE")
  CREDS=$(find "$C" -maxdepth 1 -name '*-*-*-*-*.json' | head -n1)
  if [[ -z "$CREDS" ]]; then
    if [[ ! -s "$C/cert.pem" ]]; then
      echo "  Log in to Cloudflare: open the URL below and pick $DOMAIN."
      docker run --rm -it -v "$C:/home/nonroot/.cloudflared" "$CF_IMAGE" tunnel login
    fi
    "${CF[@]}" tunnel create "$TUNNEL_NAME" >/dev/null
    CREDS=$(find "$C" -maxdepth 1 -name '*-*-*-*-*.json' | head -n1)
    [[ -n "$CREDS" ]] || die "Tunnel creation did not produce a credentials file"
    ok "created tunnel '$TUNNEL_NAME' — add $(basename "$CREDS") to your offline kit"
  fi
  TUNNEL_ID=$(basename "$CREDS" .json)
  if has_role bao && [[ -s "$C/cert.pem" ]]; then
    "${CF[@]}" tunnel route dns --overwrite-dns "$TUNNEL_ID" "bao.$DOMAIN" >/dev/null 2>&1 \
      && ok "DNS bao.$DOMAIN → tunnel" || warn "could not route bao.$DOMAIN (check the DNS record by hand)"
  elif has_role bao; then
    warn "no cert.pem, so bao.$DOMAIN DNS was not touched — fine if it already points at tunnel $TUNNEL_ID"
  fi
  if [[ -s "$C/cert.pem" ]]; then
    mv "$C/cert.pem" /root/cloudflared-cert.pem && chmod 600 /root/cloudflared-cert.pem
    ok "moved the account-level cert.pem out of the stack (/root/cloudflared-cert.pem; delete it when done)"
  fi

  {
    echo "# Rendered by bootstrap.sh"
    echo "tunnel: $TUNNEL_ID"
    echo "credentials-file: /etc/cloudflared/$TUNNEL_ID.json"
    echo "ingress:"
    if has_role bao; then
      echo "  - hostname: bao.$DOMAIN"
      echo "    service: http://openbao:8200"
    fi
    if has_role apps && [[ "$MODE" == tunnel ]]; then
      echo "  - hostname: \"*.$DOMAIN\""
      echo "    service: http://caddy:80"
    fi
    echo "  - service: http_status:404"
  } > "$C/config.yml"
  chown 65532:65532 "$C"/*
  chmod 640 "$C"/*.json
  render "$TPL/compose.cloudflared.yml" "$STACK_DIR/compose/cloudflared.yml" STACK_DIR
  INCLUDES+=(compose/cloudflared.yml)
  ok "tunnel $TUNNEL_ID configured"
fi

# ---------------------------------------------------------------- start the stack
step "Starting the stack"
{
  echo "# Rendered by bootstrap.sh (roles: $ROLES)"
  echo "name: stack"
  echo "include:"
  for f in "${INCLUDES[@]}"; do echo "  - $f"; done
} > "$STACK_DIR/compose.yml"
if (( ${#INCLUDES[@]} )); then
  (cd "$STACK_DIR" && docker compose up -d --remove-orphans --quiet-pull)
  ok "running: $(cd "$STACK_DIR" && docker compose ps --format '{{.Name}}' | paste -sd' ')"
else
  ok "nothing to run for these roles"
fi

# ---------------------------------------------------------------- bao: init or restore
if has_role bao; then
  step "OpenBao initialisation"
  BAO=(docker compose -f "$STACK_DIR/compose.yml" exec -T openbao bao)
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8200/v1/sys/health || true)
    [[ "$code" != 000 ]] && break
    sleep 2
  done
  INIT=$(curl -s http://127.0.0.1:8200/v1/sys/init | jq -r .initialized)
  if [[ "$INIT" == true ]]; then
    [[ -z "$RESTORE" ]] || warn "OpenBao is already initialised — not restoring over live data. To force: stop it, empty $STACK_DIR/openbao/file, re-run."
    ok "already initialised; sealed=$(curl -s http://127.0.0.1:8200/v1/sys/seal-status | jq -r .sealed)"
  elif [[ -n "$RESTORE" ]]; then
    TMP=$("${BAO[@]}" operator init -recovery-shares=1 -recovery-threshold=1 -format=json)
    ROOT=$(jq -r .root_token <<<"$TMP")
    SNAP="$STACK_DIR/openbao/snapshots/restore-$(date +%s).snap"
    install -m 640 -o "$BAO_UID" -g "$BAO_GID" "$RESTORE" "$SNAP"
    docker compose -f "$STACK_DIR/compose.yml" exec -T -e BAO_TOKEN="$ROOT" openbao \
      bao operator raft snapshot restore -force "/openbao/snapshots/$(basename "$SNAP")"
    sleep 3
    ok "restored $(basename "$RESTORE"); sealed=$(curl -s http://127.0.0.1:8200/v1/sys/seal-status | jq -r .sealed)"
    ok "log in with your usual account; re-issue the backup token with bin/bao-configure --backup-token"
  else
    OUT="$STACK_DIR/openbao/INIT-OUTPUT.txt"
    ( umask 077; "${BAO[@]}" operator init -recovery-shares=3 -recovery-threshold=2 > "$OUT" )
    ok "initialised. Recovery keys and root token are in $OUT"
    warn "copy $OUT to your offline kit, then: shred -u $OUT"
  fi
  if [[ "${NEW_KEY:-0}" == 1 ]]; then
    warn "NEW unseal key. Put this in your offline kit (never in Backblaze):"
    echo "      KEY_ID=$KEY_ID"
    echo "      $(base64 -w0 "$STACK_DIR/openbao/secrets/unseal.key")"
  fi
fi

# ---------------------------------------------------------------- role: runner
if has_role runner; then
  step "GitHub Actions runner user"
  id gha >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker gha
  install -d -o gha -g gha "$APPS_DIR"
  [[ -d "$STACK_DIR/caddy/sites" ]] && chown -R gha:gha "$STACK_DIR/caddy/sites"
  ok "user 'gha' (docker group) owns $APPS_DIR and caddy/sites"
  if [[ -n "$RUNNER_URL" ]]; then
    [[ -n "$RUNNER_TOKEN" ]] || die "--runner-url needs --runner-token"
    NAME="$(hostname -s)-$(slug "${RUNNER_URL##*/}")"
    DIR="/home/gha/runner-$(slug "${RUNNER_URL##*/}")"
    if [[ -f "$DIR/.runner" ]]; then
      ok "runner already configured in $DIR"
    else
      VER=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name); VER="${VER#v}"
      case "$(uname -m)" in x86_64) A=x64 ;; aarch64|arm64) A=arm64 ;; *) die "unsupported arch" ;; esac
      install -d -o gha -g gha "$DIR"
      (cd "$DIR" \
        && sudo -u gha curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v$VER/actions-runner-linux-$A-$VER.tar.gz" \
        && sudo -u gha tar xzf runner.tgz && rm -f runner.tgz \
        && sudo -u gha ./config.sh --unattended --replace --url "$RUNNER_URL" --token "$RUNNER_TOKEN" \
             --name "$NAME" --labels "$RUNNER_LABEL" \
        && ./svc.sh install gha >/dev/null && ./svc.sh start >/dev/null)
      ok "runner $NAME (label '$RUNNER_LABEL') registered and running"
    fi
  else
    ok "no runner registered here — onboard-repo.sh --runner-ssh does it per repo"
  fi
fi

# ---------------------------------------------------------------- SSH CA trust
step "SSH certificate authority"
CA=$(curl -fsS "$BAO_ADDR/v1/ssh/public_key" 2>/dev/null || true)
if [[ "$CA" == ssh-* ]]; then
  printf '%s\n' "$CA" > /etc/ssh/trusted-user-ca-keys.pem
  echo 'TrustedUserCAKeys /etc/ssh/trusted-user-ca-keys.pem' > /etc/ssh/sshd_config.d/10-openbao-ca.conf
  sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  ok "sshd trusts OpenBao-signed certificates"
else
  warn "OpenBao's SSH CA isn't reachable yet (normal on a brand-new install). Re-run this script after bin/bao-configure."
fi

# ---------------------------------------------------------------- next steps
step "Next steps"
if has_role bao && [[ -z "$RESTORE" ]] && [[ -f "$STACK_DIR/openbao/INIT-OUTPUT.txt" ]]; then
  echo "  1. Laptop: BAO_TOKEN=<root token from INIT-OUTPUT.txt> bin/bao-configure"
  echo "  2. Laptop: copy the backup token it prints into $STACK_DIR/openbao/backup.token (chmod 600)"
  echo "  3. Here:   re-run this script once to trust the SSH CA"
fi
if has_role apps; then
  if [[ "$MODE" == tunnel ]]; then
    echo "  • Laptop: bin/register-target $RUNNER_LABEL --mode tunnel --tunnel-id ${TUNNEL_ID:-<id>}"
  else
    echo "  • Laptop: bin/register-target <name> --mode public --host $(curl -fsS -4 https://ifconfig.me 2>/dev/null || echo '<public-ip>')"
  fi
fi
has_role bao && echo "  • Backblaze: include $STACK_DIR/openbao/snapshots; EXCLUDE $STACK_DIR/openbao/secrets and $STACK_DIR/cloudflared"
echo
