# infra

Self-hosted secrets and deploy pipeline: **OpenBao** holds every dev secret, **GitHub Actions** deploys
frontends to **Cloudflare Pages** and backends to any registered server, and **Cloudflare Tunnel** keeps the
OpenBao host (and optionally your apps) off the open internet.

Everything here is code; nothing here is secret. State lives in OpenBao (backed up to Backblaze), and the few
keys that unlock it live in your **offline kit**. Rebuilding any machine is: clone, fill in `config.env`,
run `bootstrap.sh`.

```
 laptop ──bao-run / bao-day──┐
                             ▼
 GitHub Actions ──OIDC──▶ OpenBao (bao.<DOMAIN>, via tunnel) ──SSH CA──▶ servers trust 10-min certs
      │
      ├─▶ Cloudflare Pages       <sub>.<DOMAIN>            (deploy-pages.yml)
      ├─▶ public target (VPS)    <repo>-backend.<DOMAIN>   (deploy-backend.yml, over SSH, Caddy on :443)
      └─▶ tunnel target (home)   <repo>-backend.<DOMAIN>   (deploy-backend.yml, SSH through the tunnel)
```

## Layout

| Path | What it is |
| --- | --- |
| `config.env.example` | Copy to `config.env` (git-ignored): domain, GitHub owner, versions, paths |
| `bootstrap.sh` | Sets up or rebuilds a server by role: `bao`, `apps` (and optionally `runner`) |
| `stack/templates/` | Compose, OpenBao, Caddy and snapshot templates that `bootstrap.sh` renders |
| `.github/workflows/` | Reusable `deploy-pages.yml` and `deploy-backend.yml` that app repos call |
| `bin/bao-configure` | Applies the OpenBao config: engines, SSH CA, policies, JWT auth, your login |
| `bin/register-target` | Registers a deploy target (a server) in OpenBao |
| `bin/onboard-repo.sh` | Wires an app repo into the pipeline |
| `bin/bao-day`, `bao-run`, `bao-ssh-cert` | Daily laptop helpers |
| `examples/` | What the generated caller workflows look like |

## Concepts

**Roles** (per machine, combine freely):
- `bao`: OpenBao with static-key auto-unseal, behind this machine's tunnel at `bao.<DOMAIN>`, plus a nightly snapshot.
- `apps --mode tunnel`: Caddy behind the tunnel. No open ports: GitHub deploys over SSH through the tunnel at
  `ssh-<TUNNEL_NAME>.<DOMAIN>`, which Cloudflare Access only opens for a service token.
- `apps --mode public`: Caddy on public `:443` with a Cloudflare Origin cert. GitHub deploys over SSH as `deploy`.
- `runner` (optional): a `gha` user for a self-hosted GitHub Actions runner. Deploys don't need one.

**Targets** are named servers in `kv/shared/targets/<name>` (e.g. `home`, `oracle`). A backend repo's
`DEPLOY_TARGET` variable picks one. Every deploy runs on GitHub's own runners and logs in as `deploy` with
a 10-minute OpenBao certificate, so public repos are safe to deploy.

**Offline kit** (print + USB, kept in two places, never in Backblaze):
- the unseal key (base64) and its KEY_ID
- the 3 recovery keys (and the initial root token until you revoke it)
- each machine's tunnel credentials file `cloudflared/<TUNNEL_ID>.json`
- your OpenBao password

---

## 1. First-time setup

### 1.1 Home server (OpenBao + apps)

Ubuntu 24.04 with a sudo user. Your domain's DNS must be on Cloudflare.

```bash
git clone https://github.com/<GH_OWNER>/infra.git ~/infra && cd ~/infra
cp config.env.example config.env && nano config.env
sudo ./bootstrap.sh --role bao,apps
```

The script installs Docker if needed and asks you to log in to Cloudflare once to create the tunnel. It then
initialises OpenBao and prints the things that belong in your offline kit:
- the unseal key
- `INIT-OUTPUT.txt`, with the recovery keys and the root token
- the tunnel credentials file

Move them there, then `shred -u /opt/stack/openbao/INIT-OUTPUT.txt`.

### 1.2 Laptop

```bash
git clone https://github.com/<GH_OWNER>/infra.git ~/infra && cd ~/infra
cp config.env.example config.env && nano config.env          # same values as the server
brew install openbao jq gh                                    # or the .deb from OpenBao releases
echo 'export BAO_ADDR=https://bao.<DOMAIN>'  >> ~/.bashrc
echo 'export PATH="$HOME/infra/bin:$PATH"'   >> ~/.bashrc && source ~/.bashrc

BAO_TOKEN=<root token> bao-configure --server you@homeserver  # prompts for your password + Cloudflare values
bao login -method=userpass username=me
bao token revoke <root token>
ssh -t you@homeserver 'cd ~/infra && sudo ./bootstrap.sh --role bao,apps'   # re-run: trusts the SSH CA now
```

`bao-configure` creates the Cloudflare API token entry. The token needs **Account · Cloudflare Pages · Edit**,
**Zone · DNS · Edit** and **Zone · Zone · Read**. For a GHCR pull token, use a classic PAT with only
`read:packages`, or leave it empty if your images are public.

### 1.3 Register the home server as a target

`bootstrap.sh` printed the exact command; it looks like:

```bash
bao-ssh-cert                                                       # lets the laptop SSH in with a cert
register-target home --mode tunnel --tunnel-id <TUNNEL_ID> --ssh you@homeserver
```

The first tunnel target asks for a Cloudflare Access **service token** and prints where to make it:
1. **Zero Trust → Access → Service credentials → Service Tokens → Create**, named `github-deploy`.
   Copy the Client ID and Secret.
2. **Zero Trust → Access → Applications → Add → Self-hosted** for `ssh-<TUNNEL_NAME>.<DOMAIN>`, with a
   **Service Auth** policy that includes that token.

`register-target` also pins the server's SSH host key and creates the `ssh-…` DNS record. Without the Access
application, anyone could reach your server's SSH through the tunnel, so create it before the first deploy.

### 1.4 Add a public app server (e.g. Oracle VPS)

On the VPS:

```bash
git clone https://github.com/<GH_OWNER>/infra.git ~/infra && cd ~/infra
cp config.env.example config.env && nano config.env
sudo ./bootstrap.sh --role apps --mode public
```

In your cloud firewall (e.g. the OCI security list), allow TCP 443, ideally only from
[Cloudflare's IP ranges](https://www.cloudflare.com/ips/). Then, on the laptop:

```bash
bao-ssh-cert                                                   # your 12h cert; the VPS now trusts it
register-target oracle --mode public --host <VPS_IP>           # pins host key, installs Origin cert
```

The first run asks for a Cloudflare Origin certificate. Create it under SSL/TLS → Origin Server for
`*.<DOMAIN>, <DOMAIN>`, and set SSL/TLS mode to **Full (strict)**. Once it works, remove
`~/.ssh/authorized_keys` on the VPS so only certificates are accepted.

### 1.5 Git pushes (GitHub key lives only in OpenBao)

```bash
ssh-keygen -t ed25519 -C github -N "" -f /tmp/gh && cat /tmp/gh.pub   # add to GitHub → SSH keys
bao kv put -mount=kv ssh/github private_key=@/tmp/gh public_key=@/tmp/gh.pub && shred -u /tmp/gh /tmp/gh.pub
```

### 1.6 Backups

Add `/opt/stack/openbao/snapshots` and any app data folders (`/opt/apps/*/data`) to your Backblaze job. **Exclude** `/opt/stack/openbao/secrets` and
`/opt/stack/cloudflared`. Snapshots run at 02:30 (`/var/log/bao-snapshot.log`); schedule Backblaze after that.

---

## 2. Onboarding repos

```bash
bao-day                                                        # once a day
cd ~/code/my-site && onboard-repo.sh --frontend
onboard-repo.sh jdoe/api --backend --target oracle --prod-env .env.prod --dev-env .env
onboard-repo.sh jdoe/worker --backend --target home --dockerfile server/Dockerfile --data-dir /app/data
onboard-repo.sh jdoe/api --backend --target oracle --pr --dry-run   # preview, then open a PR
onboard-repo.sh jdoe/api --backend --target oracle --gate-on-ci     # deploy only after the repo's CI passes
```

It imports secrets into `kv/apps/<owner>/<repo>/{prod,dev}` and sets the `BAO_ADDR` and `DEPLOY_TARGET`
repo variables, and commits `.github/workflows/deploy.yml` (or opens a PR).

- **Backends** need a `Dockerfile` whose app listens on `0.0.0.0:$PORT` (default 8080). Use `--dockerfile
  server/Dockerfile` for one in a subfolder (the build context stays the repo root unless you pass `--context`),
  `--port` for another port, and `--subdomain` to serve at a name other than `<repo>-backend`.
- **Persistent data**: `--data-dir /app/data` keeps that container path in `/opt/apps/<repo>/data` on the
  server across deploys. Add `/opt/apps/*/data` to your Backblaze job. **A rollback (below) reuses the same
  data directory**: if the new image changed the data (a database migration, say), the old image starts
  against the changed data. Infra can't undo that, so keep data changes backward compatible for one release,
  or take your own backup before migrating.
- **Health checks and rollback**: give every backend a cheap, unauthenticated health endpoint and pass
  `--health-path /api/health` (also `--health-timeout SECS`, default 90, and `--health-stable SECS`, default 10).
  See [Backend health checks](#backend-health-checks-and-rollback).
- **Frontends** build with `npm ci && npm run build` into `dist/` unless you pass other options.
- **Gating deploys behind CI**: by default `deploy.yml` runs on every push to `main`, even one that fails
  the repo's tests. With `--gate-on-ci` it is triggered by `workflow_call` instead, and the repo's own CI
  calls it as its last job (the script checks for this and prints the job if it's missing):
  ```yaml
    deploy:
      needs: [test, lint]          # the CI jobs that must pass first
      if: github.event_name == 'push' && github.ref == 'refs/heads/main'
      uses: ./.github/workflows/deploy.yml
      permissions: { contents: read, packages: write, id-token: write }   # frontends: { contents: read, id-token: write }
      secrets: inherit
  ```
  Without that job, pushes stop deploying. A push that fails CI shows `deploy` as skipped. **Actions → deploy →
  Run workflow** still deploys straight away without CI, for redeploys and rollbacks. The OpenBao `deploy` role
  only binds `repository_owner` and `ref`, so the extra level of nesting (`ci.yml` → `deploy.yml` →
  `deploy-backend.yml`) authenticates the same way. Pass `--gate-on-ci` on every re-run, or the trigger
  reverts to push.
- **Moving a backend** to another target is `gh variable set DEPLOY_TARGET -R owner/repo --body home`
  and a re-run; then `docker compose down` in `/opt/apps/<repo>` on the old server.

### Backend health checks and rollback

`docker compose up -d` returns as soon as the container exists, so on its own a container that crashes on
startup would look like a successful deploy. `deploy-backend.yml` therefore runs one script on the target that
pulls the image, saves the live `compose.yml`, `app.env` and Caddy site file as `*.prev` in `/opt/apps/<repo>`,
starts the new image, and polls every 2 seconds for up to `health_timeout` seconds. The new container is
**healthy** when all of these hold at the same time:

- its status is `running` (not `restarting`), and its restart count hasn't gone up since the wait began
  (a single restart fails the check straight away);
- it has been up for at least `health_stable` seconds, so a crash a few seconds after starting is caught;
- with `health_path` set, `GET http://<repo>:<port><health_path>` returns 2xx. The request goes from a
  throwaway `curlimages/curl` container on the `edge` network, so app images don't need `curl`.

Without `health_path` only the first two are checked: a running app that answers 500 still passes.

**If it's healthy**, the site file is installed, Caddy reloads, and older local tags of
`ghcr.io/<owner>/<repo>` are removed except the new image and the one it replaced (kept for rollbacks).

**If it isn't**, the job prints the container's state (status, exit code, restart count, error) and
`docker logs --tail 100`, then:

| Situation | What happens | Summary says |
| --- | --- | --- |
| A previous deployment exists | Restores `compose.yml.prev`/`app.env.prev`, starts it and runs the same health check on it. Caddy keeps the old site file. | "failed and was rolled back", with the rollback's health |
| First deploy on this target | Stops the container so it doesn't restart-loop; its files become `compose.yml.failed`/`app.env.failed` | "First deploy … failed" |
| Image or probe can't be pulled | Nothing is changed | "aborted before anything changed" |

The job **fails in every one of these cases, even when the rollback is healthy**: the deploy itself didn't
work. The step summary has the reason and the last 30 log lines (with `app.env` values blanked out); the full
100 lines are in the "Deploy over SSH" step log. Typical reasons:

- `restarted N time(s) during the health wait (crash loop)`: the app exits on startup or soon after. The logs say why.
- `not healthy within 90s: GET … -> HTTP 404` (or `500`, or `000` for no answer): it runs, but the health
  endpoint is wrong, failing, or the app isn't listening on `0.0.0.0:$PORT`.
- `not healthy within 90s: up 4s, needs 10s without a restart`: it's slow to start; raise `health_timeout`.

A rollback's health check uses the same `health_path`, so the first time you add one, an old image without
that endpoint will be reported as an unhealthy rollback even though it is serving. That case is the
exception; normally both images have the endpoint.

After the deploy, a public smoke test requests `https://<host>/` from GitHub's runner. It only **warns**:
Cloudflare Access or WAF rules can block GitHub, so the on-server check decides the result. Deploys of the
same repo to the same target queue one after another (a `concurrency` group), so their rollback bookkeeping
never interleaves; if several are queued, GitHub keeps only the newest waiting one.

The script runs **detached on the target**: each run uploads its files and the script to
`/opt/apps/<repo>/.deploys/<run id>-<attempt>/`, starts it with `setsid nohup`, and the job only follows its
`log` (reconnecting if the SSH connection drops). So cancelling the job can't stop a deploy between swapping
`compose.yml` and the health check or rollback. That matters with `--gate-on-ci` when the repo's CI uses
`concurrency: … cancel-in-progress: true`, because a newer push cancels the running deploy job. The cancelled
deploy still finishes on the server, and it holds `/opt/apps/<repo>/.deploy.lock` until it does, so the
next deploy waits for it there (up to 15 minutes). A cancelled job writes no step summary; its result is the
`DEPLOY_RESULT=` line in that run's `log`. The logs of the last 10 runs are kept.

To roll back by hand on the server: `cd /opt/apps/<repo> && cp compose.yml.prev compose.yml && cp app.env.prev
app.env && docker compose up -d`. Usually it's easier to re-run an older successful deploy from Actions.

## 3. Day to day

| Task | Command |
| --- | --- |
| Start of day | `bao-day` (login, SSH cert, GitHub key) |
| Run locally with secrets | `bao-run -- npm run dev` / `bao-run prod -- ./migrate.sh` |
| Change a secret | `bao kv patch -mount=kv apps/<owner>/<repo>/prod KEY=value`, then re-run the deploy |
| SSH to a server | `ssh ubuntu@<host>` (uses the cert from `bao-day`) |
| List targets | `register-target --list` |
| Upgrade OpenBao | bump `BAO_VERSION` in `config.env`, `git pull`, `sudo ./bootstrap.sh --role …` |

---

## 4. Re-imaging or moving machines

### Re-image the OpenBao host (or move it to a VPS)

1. Copy the latest `bao-YYYY-MM-DD.snap` from Backblaze to the new machine.
2. Clone the repo and fill in `config.env` (same `DOMAIN`, `BAO_ADDR`). Set `TUNNEL_NAME` to the old tunnel's
   name to keep it.
3. From the offline kit:
   ```bash
   sudo install -d /opt/stack/openbao/secrets /opt/stack/cloudflared
   base64 -d | sudo tee /opt/stack/openbao/secrets/unseal.key >/dev/null        # paste, Ctrl-D
   sudo cp <TUNNEL_ID>.json /opt/stack/cloudflared/                              # keeps the same tunnel
   ```
4. `sudo ./bootstrap.sh --role bao,apps --restore bao-YYYY-MM-DD.snap`
5. Laptop: `bao-day` should just work. Re-issue the backup token with
   `bao-configure --backup-token --server you@newhost`.

Because the tunnel credentials moved with you, `bao.<DOMAIN>` and every tunnel-target app follow
automatically. Nothing else changes: laptop, workflows, other servers and DNS all stay the same.

**If the tunnel credentials are lost**, skip the `cp`; bootstrap creates a new tunnel and repoints
`bao.<DOMAIN>`. Then run `register-target <name> --mode tunnel --tunnel-id <new id> --ssh …` and re-run each tunnel
app's deploy so its DNS follows.

**Running apps after a move**: app containers aren't in the snapshot. Re-run each backend's deploy
(Actions → deploy → Run workflow). A rebuilt machine has a new SSH host key, so re-run `register-target` first.

### Rebuild a public app server

`sudo ./bootstrap.sh --role apps --mode public`, then `register-target <name> --mode public --host <ip>`
(the host key changed), then re-run each backend's deploy.

### Test your restore

Do it once on a spare VM with a copy of the kit. It takes about 10 minutes and proves the backups are good.

---

## Security notes

- **Static-key auto-unseal** means root on the OpenBao host can decrypt everything. Backups are safe
  because the key never goes to Backblaze. You can migrate to a KMS or transit seal later without touching
  your secrets.
- **GitHub Actions tokens** last 10 minutes. They only work for repos owned by `GH_OWNER` running on `main`,
  and can only read `kv/shared/*` and their own `kv/apps/<repo>/prod`. Keep anything more sensitive out of
  `kv/shared`.
- **Deploy SSH** is user `deploy` (Docker access, so root-equivalent) and only accepts OpenBao certificates.
  Only `main` of repos owned by `GH_OWNER` can get those certificates, valid for 10 minutes. On tunnel targets,
  Cloudflare Access also requires the service token before SSH is even reachable.
- **If you add a self-hosted runner** anyway, use it only for private repos. Pull requests from forks of a
  public repo can run code on it.
- **The OpenBao API is public** (CI needs it). Put Cloudflare Access on the `/ui` path, and add a WAF rate
  limit on `/v1/auth/`.
- **Remote commands** in `register-target` and `bao-configure --server` use `sudo` over SSH, so they need
  passwordless sudo or a cached sudo session on the target (default on OCI images).
