# MBP Laptop Baseline — Full Handoff (2026-11-17)

This is the complete context for a new `opencode` session in `enmaas-mini-laptop-2` (`yossiovadia/feat/laptop-mbp-baseline`). The previous session was moved here from `periwinkle` (docs).

## 1. Project & Goal

- **enmaas-mini / periwinkle** is the standalone single-SKU EnMaaS (vs EnMaaS-opps on ROSA). Two namespaces `enmaas` + `enmaas-monitoring` collapse to one RHEL 9.6 host with Podman Quadlets (`praxis-svc`), hardened Postgres, MaaS + PriceTag + Praxis.
- **Goal:** Production-grade, repeatable `deploy --from-scratch` ↔ `teardown --purge-volumes` on your **M4 Pro MBP (14c/48GiB, macOS 26.7.2, podman-machine applehv 10vCPU/16GiB/93GiB, Fedora 41)** as the baseline for feature/regression testing. No hacks.
- **Provider simulation:** Your `yossiovadia/llm-katan v0.22.0` (echo backend, `openai,anthropic,vertex,bedrock,azure` on `:8000`, failure injection flags) replaces `tests/common/provider.py` fixture for determinism (`2+3=5` tokens).

## 2. Workspaces & Branches

- **Orca worktrees:**
  - `periwinkle` → `yossiovadia/docs/production-readiness-comparison` @055605d → PR #66 to `redhat-et/enmaas-mini` (docs + `enmaas-mini-flow.html` visualizer at `https://redhat-et.github.io/enmaas-mini/architecture/enmaas-mini-flow.html`). Clean, no laptop commits.
  - `enmaas-mini-laptop-2` → `yossiovadia/feat/laptop-mbp-baseline` @bf75661 (this workspace). 6 commits: `feat(laptop)` + 5 `fix(laptop):` (disk absolute GiB, compose duplicate mount, llm-katan opt-in, maas published image, fixture fallback).
- **MBP clone:** `~/code/enmaas-mini` on `yovadia-mac` tracks `fork/yossiovadia/feat/laptop-mbp-baseline` (pulled via `gh auth --insecure-storage` so Tailscale SSH can read `hosts.yml`).
- **Remote docs repo:** `noyitz/ai-gateway-docs` PR #3 adds `enmaas-mini-flow.html` (mirror of `enmaas-flow.html`, single-host RHEL with `10.77.0.0/24` + `10.78.0.0/24` isolation).

## 3. Laptop Access (Tailscale)

- **MBP:** `yovadia@yovadia-mac.ghost-escalator.ts.net` — passwordless SSH, `zsh`, `brew` at `/opt/homebrew/bin`, `podman 5.8.0`, `podman-compose 1.5.0`, `orca 1.4.261`, `gh 2.80.0`, `Docker.app` present but idle.
- **Podman VM:** `podman-machine-default` applehv, Fedora 41, 10 vCPU / 16GiB / 93GiB (bumped from 7/3.7GiB), `135 GiB free / 926 GiB total` (14.6% free — gate now checks `>=20 GiB absolute` not 30%, via `scripts/laptop/deploy:339` fix).
- **GH auth:** `~/.config/gh/hosts.yml` now `oauth_token: gho_****` via `gh auth login --insecure-storage` — non-interactive SSH works (keychain `login.keychain-db` was locked over SSH before).
- **Test command:** `ssh yovadia@yovadia-mac.ghost-escalator.ts.net 'gh auth status; gh api user --jq .login'`

## 4. What Works (on MBP right now)

- `postgres:17.8` healthy on `5432`, `metering` healthy on `18087` (`/ready` → 200).
- `scripts/laptop/deploy` (87 KB, `py_compile` OK) — 6 stages mirroring `install.py`, platform dispatch `ID==rhel`→Quadlet else Darwin compose, `arch x86_64/aarch64`, `podman>=4.6`, disk absolute GiB gate, `flock /tmp/praxis-laptop-deploy.lock`.
- `scripts/laptop/teardown` (52 KB) — `podman-compose down`, `volume rm pgdata/pricetag-data`, `network rm pricetag-private/mock`, `secret rm`, `rm -rf ADMIN/DEPLOY`, idempotent, `--dry-run/--purge-volumes/--force`.
- `configs/laptop/llm-katan.env` (66 lines) — echo `fixture` model, `openai,anthropic` on `:8000`, synthetic keys, all `ERROR_RATE/LATENCY_MS` disabled.
- `scripts/laptop/compose.laptop.yaml.j2` (319 lines) — `pricetag-data` volume, `pricetag-private 10.77.0.0/24` + `pricetag-mock 10.78.0.0/24` isolated, `pricetag-m2m` + `pricetag-mock-ui-v1` secrets, 6 healthchecks.
- Published images pull via Rosetta (Praxis `arm64` OK, MaaS/metering `amd64` warn but continue).

## 5. What Remains (not yet bullet-proof)

- **Postgres volume perms:** `chmod/chown Operation not permitted` on `pricetag-data:/var/lib/postgresql/data` (rootless volume owned by 1001, Postgres UID 70). Fix: pre-create volume with correct owner or run postgres without `cap_drop ALL`.
- **MaaS DB URL symlink:** `database URL file must be regular file, not symlink` — `compose.laptop.yaml` mounts `./maas-db-url:/run/secrets/maas-db-url:ro` relative to repo root, but file lives in `~/.local/share/enmaas-mini/laptop/maas-db-url`. Need absolute `deploy_dir/maas-db-url` bind.
- **Default provider:** `llm-katan` private `ghcr.io` 403 → now opt-in, default is fixture `tests/common/provider.py` (Python `python:3.12` on `10.78.0.10` with `2+3=5` tokens). That path is next to verify.
- **Verification:** `deploy --verify` should run `harness` + `benchmark --requests 20` (720 req, 2+3=5 ledger reconciliation), `rhel-quota.py 200→429`, `observe` disk 30% floor. Not yet green loop-to-loop.

## 6. How to Proceed

### In macOS Terminal (Orca worktree `enmaas-mini-laptop-2`):

```bash
# Check you're in the right worktree
pwd  # /Users/mooki/orca/workspaces/enmaas-mini/enmaas-mini-laptop-2
git status
git log --oneline -n 3  # bf75661 is latest laptop baseline

# Start opencode in this worktree (fresh session will read this HANDOFF.md)
opencode

# Or one-shot
opencode run "continue fixing laptop deploy: postgres volume perms + maas-db-url absolute path, then deploy --from-scratch --verify till green"
```

### Inside `opencode` (this agent):

```
You are in enmaas-mini-laptop-2 (yossiovadia/feat/laptop-mbp-baseline).
Read HANDOFF.md and scripts/laptop/deploy:700, compose.laptop.yaml.j2:120.
Fix: 1) postgres volume pre-create with correct UID, 2) maas-db-url absolute bind, 3) ensure fixture provider is default.
Then on yovadia-mac (Tailscale) run:
  ssh yovadia@yovadia-mac.ghost-escalator.ts.net
  cd ~/code/enmaas-mini && git pull --ff-only
  ./scripts/laptop/teardown --purge-volumes --yes
  ./scripts/laptop/deploy --mode mock --hostname localhost --from-scratch --yes
  # expect pricetag-{db,maas,metering,gateway,provider} Up (healthy) + curl -k https://localhost:8443/login 200
  ./scripts/laptop/deploy --verify  # harness + benchmark 20
Loop till teardown --purge && deploy --from-scratch --verify is repeatable.
```

### Useful MBP checks:

```bash
ssh yovadia@yovadia-mac.ghost-escalator.ts.net 'gh auth status; podman machine list; podman ps --format "{{.Names}} {{.Status}}"; curl -k https://localhost:8443/login -o /dev/null -w "%{http_code}\n"'
ssh yovadia@yovadia-mac.ghost-escalator.ts.net 'tail -n 100 /tmp/praxis-laptop-deploy-*.log | tail'
```

### Repo hygiene:

- Keep `periwinkle` for docs PR #66 only; keep `enmaas-mini-laptop-2` for laptop baseline (future PR to `redhat-et/enmaas-mini` when `deploy --verify` is green).
- Hardened `postgres:17.8` is placeholder; swap to `registry.redhat.io/rhel10/postgresql-17` once `lburgazzoli` confirms tag — single line in `scripts/laptop/deploy:76`.
- llm-katan is opt-in (`--with-llm-katan --llm-katan-backend echo`); default mock stays fixture for deterministic ledger.

---
*Generated by Orca session ses_e1572e944ffe13PVxWjzKm3IXy (minimac) on 2026-11-17 for handoff to MBP laptop workspace.*
