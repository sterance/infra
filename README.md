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
      └─▶ tunnel target (home)   <repo>-backend.<DOMAIN>   (deploy-backend.yml, self-hosted runner, via tunnel)
```

## Layout

| Path | What it is |
| --- | --- |
| `config.env.example` | Copy to `config.env` (git-ignored): domain, GitHub owner, versions, paths |
| `bootstrap.sh` | Sets up or rebuilds a server by role: `bao`, `apps`, `runner` |
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
- `apps --mode tunnel`: Caddy behind the tunnel. No open ports; deploys run on a self-hosted runner on the machine.
- `apps --mode public`: Caddy on public `:443` with a Cloudflare Origin cert. GitHub deploys over SSH as `deploy`.
- `runner`: the `gha` user that runs a self-hosted GitHub Actions runner (needed for tunnel-mode apps).

**Targets** are named servers in `kv/shared/targets/<name>` (e.g. `home`, `oracle`). A backend repo's
`DEPLOY_TARGET` variable picks one. For tunnel targets, the name is also the runner label.

**Offline kit** (print + USB, kept in two places, never in Backblaze):
- the unseal key (base64) and its KEY_ID
- the 3 recovery keys (and the initial root token until you revoke it)
- each machine's tunnel credentials file `cloudflared/<TUNNEL_ID>.json`
- your OpenBao password

---

## 1. First-time setup

### 1.1 Home server (OpenBao + apps + runner)

Ubuntu 24.04 with a sudo user. Your domain's DNS must be on Cloudflare.

```bash
git clone https://github.com/<GH_OWNER>/infra.git ~/infra && cd ~/infra
cp config.env.example config.env && nano config.env
sudo ./bootstrap.sh --role bao,apps,runner
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
ssh you@homeserver 'cd ~/infra && sudo ./bootstrap.sh --role bao,apps,runner'   # re-run: trusts the SSH CA now
```

`bao-configure` creates the Cloudflare API token entry. The token needs **Account · Cloudflare Pages · Edit**,
**Zone · DNS · Edit** and **Zone · Zone · Read**. For a GHCR pull token, use a classic PAT with only
`read:packages`, or leave it empty if your images are public.

### 1.3 Register the home server as a target

`bootstrap.sh` printed the exact command; it looks like:

```bash
register-target home --mode tunnel --tunnel-id <TUNNEL_ID> --ssh you@homeserver
```

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
onboard-repo.sh jdoe/worker --backend --target home --runner-ssh you@homeserver
onboard-repo.sh jdoe/api --backend --target oracle --pr --dry-run   # preview, then open a PR
```

It imports secrets into `kv/apps/<owner>/<repo>/{prod,dev}` and sets the `BAO_ADDR` and `DEPLOY_TARGET`
repo variables. It commits `.github/workflows/deploy.yml` (or opens a PR), and registers a runner for
tunnel targets.

- **Backends** need a `Dockerfile` whose app listens on `0.0.0.0:$PORT` (default 8080). Use `--dockerfile
  server/Dockerfile` for one in a subfolder (the build context stays the repo root unless you pass `--context`),
  `--port` for another port, and `--subdomain` to serve at a name other than `<repo>-backend`.
- **Persistent data**: `--data-dir /app/data` keeps that container path in `/opt/apps/<repo>/data` on the
  server across deploys. Add `/opt/apps/*/data` to your Backblaze job.
- **Frontends** build with `npm ci && npm run build` into `dist/` unless you pass other options.
- **Moving a backend** to another target is `gh variable set DEPLOY_TARGET -R owner/repo --body home`
  and a re-run; then `docker compose down` in `/opt/apps/<repo>` on the old server.

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
4. `sudo ./bootstrap.sh --role bao,apps,runner --restore bao-YYYY-MM-DD.snap`
5. Laptop: `bao-day` should just work. Re-issue the backup token with
   `bao-configure --backup-token --server you@newhost`.

Because the tunnel credentials moved with you, `bao.<DOMAIN>` and every tunnel-target app follow
automatically. Nothing else changes: laptop, workflows, other servers and DNS all stay the same.

**If the tunnel credentials are lost**, skip the `cp`; bootstrap creates a new tunnel and repoints
`bao.<DOMAIN>`. Then run `register-target <name> --mode tunnel --tunnel-id <new id>` and re-run each tunnel
app's deploy so its DNS follows.

**Running apps after a move**: app containers aren't in the snapshot. Re-run each backend's deploy
(Actions → deploy → Run workflow), and re-register the runners (`onboard-repo.sh … --runner-ssh`).

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
- **Self-hosted runners** have Docker access, so they're root-equivalent on that machine. Use them only for
  private repos you control. `onboard-repo.sh` refuses public repos on tunnel targets.
- **The OpenBao API is public** (CI needs it). Put Cloudflare Access on the `/ui` path, and add a WAF rate
  limit on `/v1/auth/`.
- **Remote commands** in `register-target` and `bao-configure --server` use `sudo` over SSH, so they need
  passwordless sudo or a cached sudo session on the target (default on OCI images).
