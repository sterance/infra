#!/usr/bin/env bash
# onboard-repo.sh — wire a GitHub repo into the OpenBao + Cloudflare deploy pipeline.
#
# Run from your laptop after `bao-day` (needs: gh, bao, jq, git, base64; ssh for --runner-ssh).
# Reads config.env from this infra repo. Idempotent: safe to re-run on the same repo.
#
# Examples
#   bin/onboard-repo.sh --frontend                                   # repo from current dir
#   bin/onboard-repo.sh jdoe/site --frontend --subdomain www --output-dir build
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --prod-env .env.prod --dev-env .env
#   bin/onboard-repo.sh jdoe/worker --backend --target home --runner-ssh me@homeserver
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --pr --dry-run

set -euo pipefail
# shellcheck source=../lib/common.sh disable=SC2154
source "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/../lib/common.sh"

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOF'

Options
  [owner/repo]            Repo to onboard (default: origin of the current git repo)
  --frontend | --backend  Pipeline type (required)
  Frontend:
    --subdomain NAME      Default: repo name (backends: <repo>-backend)
    --output-dir DIR      Default (workflow): dist
    --build-cmd CMD       Default (workflow): "npm ci && npm run build"
    --node VERSION        Default (workflow): 22
  Backend:
    --target NAME         A target registered with bin/register-target (sets DEPLOY_TARGET)
    --port N              Container port (default 8080)
    --dockerfile PATH     Dockerfile path from the repo root (default Dockerfile)
    --context DIR         Docker build context (default .)
    --data-dir PATH       Container path to persist across deploys, e.g. /app/data
    --runner-ssh U@HOST   Tunnel targets on personal accounts: register this repo's runner over SSH
  Secrets:
    --prod-env FILE       Import KEY=VALUE file into kv/apps/<owner>/<repo>/prod
    --dev-env FILE        Import KEY=VALUE file into kv/apps/<owner>/<repo>/dev
  Behaviour:
    --pr                  Open a pull request instead of committing to the default branch
    -n, --dry-run         Show what would happen, change nothing
    -y, --yes             Don't ask for confirmation
    -h, --help
EOF
}

REPO_ARG="" ; KIND="" ; TARGET="" ; SUBDOMAIN="" ; OUTPUT_DIR="" ; BUILD_CMD=""
NODE_VERSION="" ; PORT="" ; DEV_ENV="" ; PROD_ENV="" ; RUNNER_SSH="" ; USE_PR=0 ; DRY=0 ; YES=0 ; T_MODE=""
DOCKERFILE="" ; CONTEXT="" ; DATA_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --frontend) KIND=frontend ;;
    --backend) KIND=backend ;;
    --target) TARGET="$2"; shift ;;
    --subdomain) SUBDOMAIN="$2"; shift ;;
    --output-dir) OUTPUT_DIR="$2"; shift ;;
    --build-cmd) BUILD_CMD="$2"; shift ;;
    --node) NODE_VERSION="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --dockerfile) DOCKERFILE="$2"; shift ;;
    --context) CONTEXT="$2"; shift ;;
    --data-dir) DATA_DIR="$2"; shift ;;
    --prod-env) PROD_ENV="$2"; shift ;;
    --dev-env) DEV_ENV="$2"; shift ;;
    --runner-ssh|--home-ssh) RUNNER_SSH="$2"; shift ;;
    --pr) USE_PR=1 ;;
    -n|--dry-run) DRY=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) REPO_ARG="$1" ;;
  esac
  shift
done

# shellcheck disable=SC2154  # c_dim/c_off come from lib/common.sh
run() { if (( DRY )); then printf '  %s[dry-run]%s %s\n' "$c_dim" "$c_off" "$*"; else "$@"; fi; }

# Parse a dotenv file into a JSON object (KEY=VALUE, optional `export`, quotes stripped, # comments skipped).
env_to_json() {
  local file="$1" line key val json='{}'
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    [[ "$line" != *=* ]] && { warn "skipping malformed line in $file: ${line:0:30}…"; continue; }
    key="${line%%=*}"; val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { warn "skipping invalid key '$key' in $file"; continue; }
    if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
    json=$(jq -c --arg k "$key" --arg v "$val" '. + {($k): $v}' <<<"$json")
  done < "$file"
  printf '%s' "$json"
}

# Create or update a file in a repo via the GitHub API. Args: repo branch path content message
gh_put_file() {
  local repo="$1" branch="$2" path="$3" content="$4" msg="$5" sha current
  current=$(gh api "repos/$repo/contents/$path?ref=$branch" 2>/dev/null || true)
  sha=$(jq -r '.sha // empty' <<<"$current" 2>/dev/null || true)
  if [[ -n "$sha" && "$(jq -r .content <<<"$current" | base64 -d 2>/dev/null)" == "$content" ]]; then
    ok "$path already up to date on $branch"; return 0
  fi
  local args=(-X PUT "repos/$repo/contents/$path" -f "message=$msg" -f "branch=$branch"
              -f "content=$(printf '%s' "$content" | base64 | tr -d '\n')")
  [[ -n "$sha" ]] && args+=(-f "sha=$sha")
  run gh api "${args[@]}" >/dev/null
  if [[ -n "$sha" ]]; then ok "updated $path on $branch"; else ok "created $path on $branch"; fi
}

set_var() {  # set_var NAME VALUE
  local cur; cur=$(gh variable get "$1" -R "$FULL" 2>/dev/null || true)
  if [[ "$cur" == "$2" ]]; then ok "$1 already '$2'"; return 0; fi
  run gh variable set "$1" -R "$FULL" --body "$2"
  ok "$1=$2${cur:+ (was '$cur')}"
}

# ---------------------------------------------------------------- preflight
step "Preflight"
load_config
need gh bao jq git base64
[[ -z "$RUNNER_SSH" ]] || need ssh
export BAO_ADDR
gh auth status >/dev/null 2>&1 || die "gh is not logged in (run: gh auth login)"
bao token lookup >/dev/null 2>&1 || die "No valid OpenBao token (run: bao-day)"
ok "gh and bao are authenticated"

[[ -n "$KIND" ]] || die "Choose --frontend or --backend"
if [[ "$KIND" == backend ]]; then
  [[ -n "$TARGET" ]] || die "--target is required for backends (bin/register-target --list)"
  T_JSON=$(bao kv get -mount=kv -format=json "shared/targets/$TARGET" 2>/dev/null) \
    || die "Unknown target '$TARGET'. Registered: $(bao kv list -mount=kv -format=json shared/targets 2>/dev/null | jq -r 'join(", ")' || echo none)"
  T_MODE=$(jq -r .data.data.mode <<<"$T_JSON")
else
  [[ -z "$TARGET$PORT$RUNNER_SSH$DOCKERFILE$CONTEXT$DATA_DIR" ]] || die "--target/--port/--dockerfile/--context/--data-dir/--runner-ssh are backend-only"
fi
[[ -z "$PROD_ENV" || -f "$PROD_ENV" ]] || die "Not a file: $PROD_ENV"
[[ -z "$DEV_ENV"  || -f "$DEV_ENV"  ]] || die "Not a file: $DEV_ENV"

if [[ -z "$REPO_ARG" ]]; then
  REPO_ARG=$(git remote get-url origin 2>/dev/null \
    | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##') \
    || die "No repo given and no git origin in the current directory"
fi
INFO=$(gh repo view "$REPO_ARG" --json nameWithOwner,name,owner,visibility,defaultBranchRef 2>/dev/null) \
  || die "Repo '$REPO_ARG' not found or not accessible with your gh login"
FULL=$(jq -r .nameWithOwner <<<"$INFO")          # exact case — used for the secrets path
OWNER=$(jq -r .owner.login <<<"$INFO")
NAME=$(jq -r .name <<<"$INFO")
VIS=$(jq -r .visibility <<<"$INFO")
BRANCH=$(jq -r .defaultBranchRef.name <<<"$INFO")
REPO_SLUG=$(slug "$NAME")
OWNER_TYPE=$(gh api "users/$OWNER" --jq .type)    # User | Organization

[[ "$OWNER" == "$GH_OWNER" ]] || warn "$FULL is owned by $OWNER, but OpenBao only trusts GitHub Actions from $GH_OWNER (config.env)."
[[ "$BRANCH" == main ]] || warn "Default branch is '$BRANCH'; the pipeline only deploys from 'main'."
if [[ "$KIND" == backend && "$T_MODE" == tunnel && "$VIS" == PUBLIC ]]; then
  die "$FULL is public. Tunnel targets deploy on a self-hosted runner, which must only serve private repos."
fi
gh repo view "$INFRA_REPO" >/dev/null 2>&1 || die "Infra repo '$INFRA_REPO' not found (INFRA_REPO in config.env)"

if [[ "$KIND" == frontend ]]; then URL="https://${SUBDOMAIN:-$REPO_SLUG}.$DOMAIN"
else URL="https://${SUBDOMAIN:-$REPO_SLUG-backend}.$DOMAIN"; fi

cat <<EOF

  Repo         $FULL ($VIS, default branch $BRANCH)
  Type         $KIND${TARGET:+ → $TARGET ($T_MODE)}
  URL          $URL
  Secrets      kv/apps/$FULL/{prod,dev}
  Workflows    $INFRA_REPO
  Commit mode  $([[ $USE_PR == 1 ]] && echo "pull request" || echo "direct to $BRANCH")$([[ $DRY == 1 ]] && echo "  (DRY RUN)")
EOF
if (( ! YES && ! DRY )); then
  read -rp $'\nProceed? [y/N] ' a; [[ "$a" =~ ^[Yy]$ ]] || exit 1
fi

# ---------------------------------------------------------------- 1. infra repo access
step "Shared workflows access"
if [[ "$(gh repo view "$INFRA_REPO" --json visibility --jq .visibility)" == PUBLIC ]]; then
  ok "$INFRA_REPO is public — callable from any repo"
else
  LEVEL=$(gh api "repos/$INFRA_REPO/actions/permissions/access" --jq .access_level 2>/dev/null || echo none)
  WANT=$([[ "$OWNER_TYPE" == Organization ]] && echo organization || echo user)
  if [[ "$LEVEL" == none ]]; then
    run gh api -X PUT "repos/$INFRA_REPO/actions/permissions/access" -f "access_level=$WANT" >/dev/null
    ok "allowed $WANT repos to use $INFRA_REPO"
  else
    ok "$INFRA_REPO access level: $LEVEL"
  fi
fi

# ---------------------------------------------------------------- 2. secrets
step "Secrets in OpenBao"
import_env() {   # args: env-file prod|dev
  local file="$1" env="$2" path="apps/$FULL/$2" json n
  json=$(env_to_json "$file"); n=$(jq 'length' <<<"$json")
  (( n > 0 )) || { warn "$file has no variables; skipped"; return 0; }
  if bao kv get -mount=kv "$path" >/dev/null 2>&1; then
    if (( DRY )); then printf '  %s[dry-run]%s merge %s keys into kv/%s\n' "$c_dim" "$c_off" "$n" "$path"
    else bao kv patch -mount=kv "$path" - <<<"$json" >/dev/null; fi
    ok "merged $n keys from $file into $env"
  else
    if (( DRY )); then printf '  %s[dry-run]%s write %s keys to kv/%s\n' "$c_dim" "$c_off" "$n" "$path"
    else bao kv put -mount=kv "$path" - <<<"$json" >/dev/null; fi
    ok "wrote $n keys from $file to $env"
  fi
}
[[ -n "$PROD_ENV" ]] && import_env "$PROD_ENV" prod
[[ -n "$DEV_ENV"  ]] && import_env "$DEV_ENV" dev
for env in prod dev; do
  if bao kv get -mount=kv "apps/$FULL/$env" >/dev/null 2>&1; then
    ok "$env: $(bao kv get -mount=kv -format=json "apps/$FULL/$env" | jq -r '.data.data | keys | join(", ")')"
  elif (( DRY )) && { [[ "$env" == prod && -n "$PROD_ENV" ]] || [[ "$env" == dev && -n "$DEV_ENV" ]]; }; then
    :
  else
    warn "$env: none yet (fine if the app needs none) — add with: bao kv put -mount=kv apps/$FULL/$env KEY=value"
  fi
done

# ---------------------------------------------------------------- 3. repo variables
step "Repo variables"
set_var BAO_ADDR "$BAO_ADDR"
[[ "$KIND" == backend ]] && set_var DEPLOY_TARGET "$TARGET"

# ---------------------------------------------------------------- 4. runner (tunnel targets)
if [[ "$KIND" == backend && "$T_MODE" == tunnel ]]; then
  step "Self-hosted runner (label '$TARGET')"
  if [[ "$OWNER_TYPE" == Organization ]]; then
    if gh api "orgs/$OWNER/actions/runners" --jq '.runners[].labels[].name' 2>/dev/null | grep -qx "$TARGET"; then
      ok "org-level runner labelled '$TARGET' found"
    else
      warn "no org runner labelled '$TARGET' — register one: bootstrap.sh --role runner --runner-url https://github.com/$OWNER --runner-token … --runner-label $TARGET"
    fi
  elif gh api "repos/$FULL/actions/runners" --jq '.runners[].labels[].name' 2>/dev/null | grep -qx "$TARGET"; then
    ok "repo already has a runner labelled '$TARGET'"
  elif [[ -n "$RUNNER_SSH" ]]; then
    VER=$(gh api repos/actions/runner/releases/latest --jq .tag_name); VER="${VER#v}"
    if (( DRY )); then
      printf '  %s[dry-run]%s register runner %s-%s (actions/runner %s) on %s\n' "$c_dim" "$c_off" "$TARGET" "$REPO_SLUG" "$VER" "$RUNNER_SSH"
    else
      TOKEN=$(gh api -X POST "repos/$FULL/actions/runners/registration-token" --jq .token)
      REMOTE=$(mktemp)
      cat > "$REMOTE" <<EOF
set -euo pipefail
id gha >/dev/null 2>&1 || { echo "user 'gha' missing — run bootstrap.sh --role runner first"; exit 1; }
DIR=/home/gha/runner-$REPO_SLUG
if [ -f "\$DIR/.runner" ]; then echo "runner already configured in \$DIR"; exit 0; fi
case "\$(uname -m)" in x86_64) A=x64 ;; aarch64|arm64) A=arm64 ;; *) echo "unsupported arch"; exit 1 ;; esac
install -d -o gha -g gha "\$DIR"
cd "\$DIR"
sudo -u gha curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v$VER/actions-runner-linux-\$A-$VER.tar.gz"
sudo -u gha tar xzf runner.tgz && rm -f runner.tgz
sudo -u gha ./config.sh --unattended --replace --url "https://github.com/$FULL" --token "$TOKEN" \
  --name "$TARGET-$REPO_SLUG" --labels "$TARGET"
./svc.sh install gha
./svc.sh start
EOF
      R="/tmp/onboard-runner-$$.sh"
      ssh "$RUNNER_SSH" "umask 077; cat > $R" < "$REMOTE"; rm -f "$REMOTE"
      ssh -t "$RUNNER_SSH" "sudo bash $R; rc=\$?; rm -f $R; exit \$rc"
      ok "runner $TARGET-$REPO_SLUG registered on $RUNNER_SSH"
    fi
  else
    warn "personal account: this repo needs its own runner. Re-run with --runner-ssh you@server,"
    warn "or register one by hand (Repo → Settings → Actions → Runners) with the label '$TARGET'."
  fi
fi

# ---------------------------------------------------------------- 5. caller workflow
step "Caller workflow"
WITH=('bao_addr: ${{ vars.BAO_ADDR }}')
if [[ "$KIND" == frontend ]]; then
  [[ -n "$SUBDOMAIN"    ]] && WITH+=("subdomain: \"$SUBDOMAIN\"")
  [[ -n "$OUTPUT_DIR"   ]] && WITH+=("output_dir: \"$OUTPUT_DIR\"")
  [[ -n "$BUILD_CMD"    ]] && WITH+=("build_command: \"${BUILD_CMD//\"/\\\"}\"")
  [[ -n "$NODE_VERSION" ]] && WITH+=("node_version: \"$NODE_VERSION\"")
  JOB=pages; WF=deploy-pages.yml; PERMS="{ contents: read, id-token: write }"
else
  WITH+=('target: ${{ vars.DEPLOY_TARGET }}')
  [[ -n "$PORT"       ]] && WITH+=("port: \"$PORT\"")
  [[ -n "$DOCKERFILE" ]] && WITH+=("dockerfile: \"$DOCKERFILE\"")
  [[ -n "$CONTEXT"    ]] && WITH+=("context: \"$CONTEXT\"")
  [[ -n "$DATA_DIR"   ]] && WITH+=("data_dir: \"$DATA_DIR\"")
  [[ -n "$SUBDOMAIN"  ]] && WITH+=("subdomain: \"$SUBDOMAIN\"")
  JOB=backend; WF=deploy-backend.yml; PERMS="{ contents: read, packages: write, id-token: write }"
fi

CALLER="# Managed by onboard-repo.sh — pipeline logic lives in $INFRA_REPO
name: deploy
on:
  push: { branches: [main] }
  workflow_dispatch:
permissions: $PERMS
jobs:
  $JOB:
    uses: $INFRA_REPO/.github/workflows/$WF@main
    with:"
for w in "${WITH[@]}"; do CALLER+=$'\n      '"$w"; done
CALLER+=$'\n'

printf '%s' "$CALLER" | sed 's/^/  │ /'
WF_PATH=.github/workflows/deploy.yml
if (( USE_PR )); then
  PR_BRANCH="onboard/deploy-pipeline"
  BASE_SHA=$(gh api "repos/$FULL/git/ref/heads/$BRANCH" --jq .object.sha)
  if ! gh api "repos/$FULL/git/ref/heads/$PR_BRANCH" >/dev/null 2>&1; then
    run gh api -X POST "repos/$FULL/git/refs" -f "ref=refs/heads/$PR_BRANCH" -f "sha=$BASE_SHA" >/dev/null
  fi
  gh_put_file "$FULL" "$PR_BRANCH" "$WF_PATH" "$CALLER" "ci: onboard to deploy pipeline"
  if gh pr list -R "$FULL" --head "$PR_BRANCH" --json number --jq '.[0].number' 2>/dev/null | grep -q .; then
    ok "pull request already open"
  else
    run gh pr create -R "$FULL" --base "$BRANCH" --head "$PR_BRANCH" \
      --title "Onboard to deploy pipeline" \
      --body "Adds the caller workflow for \`$INFRA_REPO/$WF\`. Merging to \`main\` deploys to $URL." >/dev/null
    ok "opened pull request — merging it triggers the first deploy"
  fi
else
  gh_put_file "$FULL" "$BRANCH" "$WF_PATH" "$CALLER" "ci: onboard to deploy pipeline"
  if [[ "$BRANCH" == main ]]; then ok "this commit to main triggers the first deploy"; fi
fi

# ---------------------------------------------------------------- done
step "Done"
echo "  URL:      $URL"
echo "  Runs:     https://github.com/$FULL/actions"
echo "  Dev:      cd into the repo, then: bao-run -- <your dev command>"
if (( DRY )); then echo "  (dry run — nothing was changed)"; fi
exit 0
