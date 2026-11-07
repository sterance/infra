#!/usr/bin/env bash
# onboard-repo.sh — wire a GitHub repo into the OpenBao + Cloudflare deploy pipeline.
#
# Run from your laptop after `bao-day` (needs: gh, bao, jq, git, base64).
# Reads config.env from this infra repo. Idempotent: safe to re-run on the same repo.
#
# Examples
#   bin/onboard-repo.sh --frontend                                   # repo from current dir
#   bin/onboard-repo.sh jdoe/site --frontend --subdomain www --output-dir build
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --prod-env .env.prod --dev-env .env
#   bin/onboard-repo.sh jdoe/worker --backend --target home --dockerfile server/Dockerfile --data-dir /app/data
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --pr --dry-run
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --gate-on-ci   # deploy from the repo's CI

set -euo pipefail
# shellcheck source=../lib/common.sh disable=SC2154
source "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/../lib/common.sh"

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOF'

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
  Secrets:
    --prod-env FILE       Import KEY=VALUE file into kv/apps/<owner>/<repo>/prod
    --dev-env FILE        Import KEY=VALUE file into kv/apps/<owner>/<repo>/dev
  Behaviour:
    --gate-on-ci          Deploy only after the repo's CI passes: deploy.yml is triggered by
                          workflow_call (from a `deploy` job in the repo's CI) instead of pushes
                          to main. Manual runs still deploy straight away. Prints the CI job to add.
    --pr                  Open a pull request instead of committing to the default branch
    -n, --dry-run         Show what would happen, change nothing
    -y, --yes             Don't ask for confirmation
    -h, --help
EOF
}

REPO_ARG="" ; KIND="" ; TARGET="" ; SUBDOMAIN="" ; OUTPUT_DIR="" ; BUILD_CMD=""
NODE_VERSION="" ; PORT="" ; DEV_ENV="" ; PROD_ENV="" ; USE_PR=0 ; DRY=0 ; YES=0 ; T_MODE=""
DOCKERFILE="" ; CONTEXT="" ; DATA_DIR="" ; GATE_ON_CI=0
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
    --runner-ssh|--home-ssh) warn "$1 is no longer needed: all targets deploy over SSH from GitHub's runners"; shift ;;
    --gate-on-ci) GATE_ON_CI=1 ;;
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
  [[ -z "$TARGET$PORT$DOCKERFILE$CONTEXT$DATA_DIR" ]] || die "--target/--port/--dockerfile/--context/--data-dir are backend-only"
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
if [[ "$KIND" == backend && "$T_MODE" == tunnel ]] && ! bao kv get -mount=kv shared/cloudflare-access >/dev/null 2>&1; then
  die "Tunnel target '$TARGET' has no Access service token yet — re-run: register-target $TARGET --mode tunnel …"
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
  Trigger      $([[ $GATE_ON_CI == 1 ]] && echo "the repo's CI (workflow_call), after checks pass" || echo "push to main")
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

# ---------------------------------------------------------------- 4. caller workflow
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

if (( GATE_ON_CI )); then
  TRIGGER="# Pushes to main reach this through ci.yml's deploy job, after CI passes.
# A manual run deploys straight away, without waiting for CI.
on:
  workflow_call:
  workflow_dispatch:"
else
  TRIGGER="on:
  push: { branches: [main] }
  workflow_dispatch:"
fi

CALLER="# Managed by onboard-repo.sh — pipeline logic lives in $INFRA_REPO
name: deploy
$TRIGGER
permissions: $PERMS
jobs:
  $JOB:
    uses: $INFRA_REPO/.github/workflows/$WF@main
    with:"
for w in "${WITH[@]}"; do CALLER+=$'\n      '"$w"; done
CALLER+=$'\n'

printf '%s' "$CALLER" | sed 's/^/  │ /'
WF_PATH=.github/workflows/deploy.yml

# With --gate-on-ci nothing deploys on push until the repo's CI calls deploy.yml.
CI_SNIPPET="  deploy:
    needs: [<your check jobs>]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    uses: ./$WF_PATH
    permissions: $PERMS
    secrets: inherit"
CI_CALLS_DEPLOY=0
if (( GATE_ON_CI )); then
  for f in $(gh api "repos/$FULL/contents/.github/workflows?ref=$BRANCH" \
               --jq '.[] | select(.type == "file") | .path' 2>/dev/null || true); do
    [[ "$f" == "$WF_PATH" ]] && continue
    if gh api "repos/$FULL/contents/$f?ref=$BRANCH" --jq .content 2>/dev/null | base64 -d 2>/dev/null \
         | grep -Eq "uses:[[:space:]]*['\"]?\./$WF_PATH"; then
      ok "$f calls $WF_PATH"; CI_CALLS_DEPLOY=1
    fi
  done
  if (( ! CI_CALLS_DEPLOY )); then
    warn "No workflow on $BRANCH calls $WF_PATH yet, so pushes to main won't deploy. Add this job to the repo's CI:"
    printf '%s\n' "$CI_SNIPPET" | sed 's/^/  │ /'
  fi
fi
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
      --body "Adds the caller workflow for \`$INFRA_REPO/$WF\`. $(if (( GATE_ON_CI )); then
                echo "Deploys to $URL run from the repo's CI after its checks pass on \`main\`, or manually from Actions → deploy."
              else echo "Merging to \`main\` deploys to $URL."; fi)" >/dev/null
    if (( GATE_ON_CI )); then ok "opened pull request"
    else ok "opened pull request — merging it triggers the first deploy"; fi
  fi
else
  gh_put_file "$FULL" "$BRANCH" "$WF_PATH" "$CALLER" "ci: onboard to deploy pipeline"
  if (( GATE_ON_CI )); then
    if (( CI_CALLS_DEPLOY )); then ok "the next CI run on main that passes deploys"
    else warn "nothing deploys on push until the repo's CI has the deploy job above (Actions → deploy → Run workflow deploys now)"; fi
  elif [[ "$BRANCH" == main ]]; then ok "this commit to main triggers the first deploy"; fi
fi

# ---------------------------------------------------------------- done
step "Done"
echo "  URL:      $URL"
echo "  Runs:     https://github.com/$FULL/actions"
echo "  Dev:      cd into the repo, then: bao-run -- <your dev command>"
if (( DRY )); then echo "  (dry run — nothing was changed)"; fi
exit 0
