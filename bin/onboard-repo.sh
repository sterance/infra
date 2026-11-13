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
#   bin/onboard-repo.sh jdoe/api --backend --target oracle --health-path /api/health
#   bin/onboard-repo.sh jdoe/lib --updates                           # dependency updates only

set -euo pipefail
# shellcheck source=../lib/common.sh disable=SC2154
source "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/../lib/common.sh"

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOF'

Options
  [owner/repo]            Repo to onboard (default: origin of the current git repo)
  --frontend | --backend  Deploy pipeline type (required unless --updates is given alone)
  --updates               Weekly dependency updates (updates/README.md): writes the caller workflow,
                          a starter .github/updates.yml and the generated .github/dependabot.yml,
                          adds the repo to the updates GitHub App, turns on Dependabot alerts and
                          security updates, and offers to delete old per-repo update secrets.
                          Combines with --frontend/--backend, or alone for repos that don't deploy.
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
    --health-path PATH    HTTP path that must return 2xx before a deploy counts, e.g. /api/health
                          (recommended; without it only container state is checked)
    --health-timeout SECS Seconds to wait for the new container to be healthy (workflow default 90)
    --health-stable SECS  Seconds it must stay up with no restarts (workflow default 10)
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
DOCKERFILE="" ; CONTEXT="" ; DATA_DIR="" ; GATE_ON_CI=0 ; UPDATES=0
HEALTH_PATH="" ; HEALTH_TIMEOUT="" ; HEALTH_STABLE=""
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
    --health-path) HEALTH_PATH="$2"; shift ;;
    --health-timeout) HEALTH_TIMEOUT="$2"; shift ;;
    --health-stable) HEALTH_STABLE="$2"; shift ;;
    --prod-env) PROD_ENV="$2"; shift ;;
    --dev-env) DEV_ENV="$2"; shift ;;
    --runner-ssh|--home-ssh) warn "$1 is no longer needed: all targets deploy over SSH from GitHub's runners"; shift ;;
    --gate-on-ci) GATE_ON_CI=1 ;;
    --updates) UPDATES=1 ;;
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
  # $(…) drops trailing newlines on both sides, so files that differ only there compare equal
  if [[ -n "$sha" && "$(jq -r .content <<<"$current" | base64 -d 2>/dev/null)" == "$(printf '%s' "$content")" ]]; then
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

[[ -n "$KIND" || $UPDATES == 1 ]] || die "Choose --frontend or --backend (and/or --updates)"
(( ! GATE_ON_CI )) || [[ -n "$KIND" ]] || die "--gate-on-ci needs --frontend or --backend"
if (( UPDATES )); then
  need python3
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "--updates needs Python 3.11+"
  command -v yq >/dev/null || python3 -c 'import yaml' 2>/dev/null \
    || die "--updates reads YAML with yq (mikefarah/yq v4) or PyYAML: install one (brew install yq / pip install pyyaml)"
fi
if [[ "$KIND" == backend ]]; then
  [[ -n "$TARGET" ]] || die "--target is required for backends (bin/register-target --list)"
  T_JSON=$(bao kv get -mount=kv -format=json "shared/targets/$TARGET" 2>/dev/null) \
    || die "Unknown target '$TARGET'. Registered: $(bao kv list -mount=kv -format=json shared/targets 2>/dev/null | jq -r 'join(", ")' || echo none)"
  T_MODE=$(jq -r .data.data.mode <<<"$T_JSON")
  [[ -z "$HEALTH_PATH" || "$HEALTH_PATH" == /* ]] || die "--health-path must start with / (got '$HEALTH_PATH')"
  [[ -z "$HEALTH_TIMEOUT" || "$HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || die "--health-timeout must be whole seconds"
  [[ -z "$HEALTH_STABLE"  || "$HEALTH_STABLE"  =~ ^[0-9]+$ ]] || die "--health-stable must be whole seconds"
  (( ${HEALTH_TIMEOUT:-90} > ${HEALTH_STABLE:-10} )) || die "--health-timeout must be longer than --health-stable"
elif [[ "$KIND" == frontend ]]; then
  [[ -z "$TARGET$PORT$DOCKERFILE$CONTEXT$DATA_DIR$HEALTH_PATH$HEALTH_TIMEOUT$HEALTH_STABLE" ]] \
    || die "--target/--port/--dockerfile/--context/--data-dir/--health-* are backend-only"
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
elif [[ "$KIND" == backend ]]; then URL="https://${SUBDOMAIN:-$REPO_SLUG-backend}.$DOMAIN"
else URL="(no deploys)"; fi

cat <<EOF

  Repo         $FULL ($VIS, default branch $BRANCH)
  Type         ${KIND:-none}${TARGET:+ → $TARGET ($T_MODE)}
  Updates      $([[ $UPDATES == 1 ]] && echo "weekly dependency updates (.github/updates.yml)" || echo "not changed")
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

# Every file this run writes goes to the default branch, or with --pr to one branch and one PR.
PR_BRANCH="onboard/deploy-pipeline"
WRITE_BRANCH="$BRANCH"
WROTE=()
put_file() {   # put_file path content message
  if (( USE_PR )) && [[ "$WRITE_BRANCH" == "$BRANCH" ]]; then
    WRITE_BRANCH="$PR_BRANCH"
    if ! gh api "repos/$FULL/git/ref/heads/$PR_BRANCH" >/dev/null 2>&1; then
      local base; base=$(gh api "repos/$FULL/git/ref/heads/$BRANCH" --jq .object.sha)
      run gh api -X POST "repos/$FULL/git/refs" -f "ref=refs/heads/$PR_BRANCH" -f "sha=$base" >/dev/null
    fi
  fi
  gh_put_file "$FULL" "$WRITE_BRANCH" "$1" "$2" "$3"
  WROTE+=("$1")
}

# ---------------------------------------------------------------- 4. caller workflow
WF_PATH=.github/workflows/deploy.yml
CI_CALLS_DEPLOY=0
if [[ -n "$KIND" ]]; then
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
  [[ -n "$HEALTH_PATH"    ]] && WITH+=("health_path: \"$HEALTH_PATH\"")
  [[ -n "$HEALTH_TIMEOUT" ]] && WITH+=("health_timeout: \"$HEALTH_TIMEOUT\"")
  [[ -n "$HEALTH_STABLE"  ]] && WITH+=("health_stable: \"$HEALTH_STABLE\"")
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

# With --gate-on-ci nothing deploys on push until the repo's CI calls deploy.yml.
CI_SNIPPET="  deploy:
    needs: [<your check jobs>]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    uses: ./$WF_PATH
    permissions: $PERMS
    secrets: inherit"
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
put_file "$WF_PATH" "$CALLER" "ci: onboard to deploy pipeline"
fi

# ---------------------------------------------------------------- 5. dependency updates
if (( UPDATES )); then
  step "Dependency updates"
  CLI=(python3 "$INFRA_ROOT/updates/cli.py")
  # In infra itself, .github/workflows/dependency-updates.yml is the reusable workflow.
  if [[ "$(tr '[:upper:]' '[:lower:]' <<<"$FULL")" == "$(tr '[:upper:]' '[:lower:]' <<<"$INFRA_REPO")" ]]; then UPD_WF=.github/workflows/dependency-updates-caller.yml
  else UPD_WF=.github/workflows/dependency-updates.yml; fi
  UPD_CALLER=$("${CLI[@]}" caller --infra-repo "$INFRA_REPO" --ref main) || die "couldn't render the caller workflow"
  put_file "$UPD_WF" "$UPD_CALLER"$'\n' "ci: weekly dependency updates (infra)"

  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  mkdir -p "$TMP/repo/.github"
  if gh api "repos/$FULL/contents/.github/updates.yml?ref=$BRANCH" --jq .content 2>/dev/null | base64 -d > "$TMP/repo/.github/updates.yml" 2>/dev/null \
       && [[ -s "$TMP/repo/.github/updates.yml" ]]; then
    ok ".github/updates.yml exists; kept as it is"
  else
    "${CLI[@]}" starter --infra-repo "$INFRA_REPO" > "$TMP/repo/.github/updates.yml"
    put_file .github/updates.yml "$(cat "$TMP/repo/.github/updates.yml")"$'\n' "ci: starter .github/updates.yml"
  fi
  "${CLI[@]}" validate --root "$TMP/repo" >/dev/null || die "fix .github/updates.yml in $FULL first (above)"

  # dependabot.yml from the repo's files (as on $BRANCH, plus the files written above)
  { gh api "repos/$FULL/git/trees/$BRANCH?recursive=1" --jq '.tree[] | select(.type == "blob") | .path'
    printf '%s\n' "${WROTE[@]}"; } | sort -u > "$TMP/paths"
  DEPENDABOT=$("${CLI[@]}" dependabot --root "$TMP/repo" --paths-file "$TMP/paths") || die "couldn't render dependabot.yml"
  put_file .github/dependabot.yml "$DEPENDABOT"$'\n' "ci: generated .github/dependabot.yml (infra)"

  # The app that opens the PRs: add this repo to its installation (with your own gh login).
  if [[ -z "${UPDATES_APP:-}" ]]; then
    warn "UPDATES_APP (the GitHub App's slug) isn't set in config.env: add $FULL to the app's installation by hand"
    warn "  (GitHub → Settings → Applications → the app → Configure → Repository access)"
  else
    INST=$(gh api user/installations --paginate --jq ".installations[] | select(.app_slug == \"$UPDATES_APP\") | [.id, .repository_selection] | @tsv" 2>/dev/null | head -n1 || true)
    if [[ -z "$INST" ]]; then
      warn "no installation of the app '$UPDATES_APP' found for your gh login: install it (updates/README.md), then add $FULL"
    elif [[ "${INST#*$'\t'}" == all ]]; then
      ok "app '$UPDATES_APP' is installed on all repositories"
    else
      RID=$(gh api "repos/$FULL" --jq .id)
      if run gh api -X PUT "user/installations/${INST%%$'\t'*}/repositories/$RID" >/dev/null 2>&1; then
        ok "added $FULL to the '$UPDATES_APP' installation"
      else
        warn "couldn't add $FULL to the '$UPDATES_APP' installation (that API needs a classic personal access token)."
        warn "  Do it by hand: GitHub → Settings → Applications → $UPDATES_APP → Configure → Repository access"
      fi
    fi
  fi

  # Security advisories get a PR at once rather than on Monday.
  if run gh api -X PUT "repos/$FULL/vulnerability-alerts" >/dev/null 2>&1 \
     && run gh api -X PUT "repos/$FULL/automated-security-fixes" >/dev/null 2>&1; then
    ok "Dependabot alerts and security updates are on"
  else
    warn "couldn't turn on Dependabot alerts/security updates (repository → Settings → Advanced Security)"
  fi

  # Per-repo update secrets from before infra did this.
  OLD=$(gh secret list -R "$FULL" --json name --jq '.[].name' 2>/dev/null \
        | grep -Ex 'UPDATES_APP_CLIENT_ID|UPDATES_APP_PRIVATE_KEY|UPDATES_TOKEN' || true)
  if [[ -n "$OLD" ]]; then
    warn "old update secrets in $FULL: $(echo $OLD) (the app's key now lives in OpenBao)"
    a=n
    if (( YES )); then a=y; elif (( ! DRY )); then read -rp "  Delete them? [y/N] " a; fi
    if [[ "$a" =~ ^[Yy]$ ]] || (( DRY )); then
      for s in $OLD; do run gh secret delete "$s" -R "$FULL" && ok "deleted secret $s"; done
    else
      warn "kept them; delete later with: gh secret delete <name> -R $FULL"
    fi
  fi
fi

# ---------------------------------------------------------------- pull request
if (( USE_PR )) && (( ${#WROTE[@]} )); then
  if gh pr list -R "$FULL" --head "$PR_BRANCH" --json number --jq '.[0].number' 2>/dev/null | grep -q .; then
    ok "pull request already open"
  else
    BODY="Adds $(printf '`%s`, ' "${WROTE[@]}" | sed 's/, $//')."
    if [[ -n "$KIND" ]]; then
      if (( GATE_ON_CI )); then BODY+=" Deploys to $URL run from the repo's CI after its checks pass on \`main\`, or manually from Actions → deploy."
      else BODY+=" Merging to \`main\` deploys to $URL."; fi
    fi
    (( UPDATES )) && BODY+=" The weekly dependency check runs from $INFRA_REPO; this repo's pins go in \`.github/updates.yml\`."
    run gh pr create -R "$FULL" --base "$BRANCH" --head "$PR_BRANCH" \
      --title "Onboard to $INFRA_REPO$([[ -n "$KIND" ]] && echo " deploy pipeline")$([[ $UPDATES == 1 ]] && echo "$([[ -n "$KIND" ]] && echo " and") dependency updates")" \
      --body "$BODY" >/dev/null
    if [[ -n "$KIND" ]] && (( ! GATE_ON_CI )); then ok "opened pull request — merging it triggers the first deploy"
    else ok "opened pull request"; fi
  fi
elif [[ -n "$KIND" ]]; then
  if (( GATE_ON_CI )); then
    if (( CI_CALLS_DEPLOY )); then ok "the next CI run on main that passes deploys"
    else warn "nothing deploys on push until the repo's CI has the deploy job above (Actions → deploy → Run workflow deploys now)"; fi
  elif [[ "$BRANCH" == main ]]; then ok "this commit to main triggers the first deploy"; fi
fi

# ---------------------------------------------------------------- done
step "Done"
[[ -n "$KIND" ]] && echo "  URL:      $URL"
(( UPDATES )) && echo "  Updates:  dry run with Actions → Dependency updates → Run workflow (dry_run)"
echo "  Runs:     https://github.com/$FULL/actions"
echo "  Dev:      cd into the repo, then: bao-run -- <your dev command>"
if (( DRY )); then echo "  (dry run — nothing was changed)"; fi
exit 0
