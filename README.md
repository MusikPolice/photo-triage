# photo-triage

A self-hosted tool for exploring and cleaning up a family photo library: a zoomable similarity map, free-text search, near-duplicate and low-quality triage, face identification, and local-LLM tagging. It runs CPU-only on a home server alongside [elodie](https://github.com/jmathai/elodie). All permanent metadata is written back into the files.

- [docs/plan.md](docs/plan.md): product spec and architecture
- [docs/dev-environment.md](docs/dev-environment.md): toolchain, testing strategy, and build-time checks

> **Status:** planning complete; the Phase 1 scaffold is in progress. Steps marked 🚧 depend on files that Phase 1 adds (`mise.toml`, `justfile`, `scripts/bootstrap.sh`, `.env.example`).

---

## Developer setup (Windows host)

Development happens inside **WSL 2 (Ubuntu 24.04)**. That keeps the Python/ML stack, `exiftool`, and `ffmpeg` the same as on the Linux server. The repo, editor backend, and Claude Code all run inside WSL. Windows is only the host.

### 0. Prerequisites

- Windows 11 (or Windows 10 22H2+) with virtualization enabled in BIOS/UEFI
- Admin rights on the machine
- 16 GB+ RAM recommended (CLIP, UMAP, and InsightFace are memory-hungry at full library scale)
- Network access to the photo share (`\\192.168.2.21\pictures`) and its SMB credentials

### 1. Install WSL and Ubuntu 24.04

In an **elevated PowerShell**:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot when prompted. Ubuntu then opens and asks you to create a Linux username and password.

Confirm that it's running WSL 2:

```powershell
wsl --update
wsl -l -v          # Ubuntu-24.04 should show VERSION 2
```

**Optional: give WSL more memory.** Create `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=12GB
processors=6
```

Then run `wsl --shutdown` and reopen Ubuntu.

### 2. Install Docker Desktop with WSL integration

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and make sure the **WSL 2 based engine** is enabled.
2. Go to *Settings → Resources → WSL integration* and enable integration for **Ubuntu-24.04**.
3. In an Ubuntu terminal, verify:

   ```bash
   docker version
   docker compose version
   ```

### 3. Base packages in Ubuntu

Everything from here on runs **in the Ubuntu terminal** unless stated otherwise.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential git curl unzip cifs-utils ca-certificates
```

### 4. Git and GitHub access

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
git config --global core.autocrlf input      # the repo uses LF line endings

# GitHub CLI (used for auth and PRs)
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh

gh auth login        # GitHub.com → HTTPS → authenticate in browser
gh auth setup-git
```

### 5. Mount the photo libraries (read-only)

The app is developed against two sources of real photos. Both are mounted **read-only**, so no bug or misconfiguration during development can modify originals.

| Source | Windows location | WSL mount |
|---|---|---|
| Curated sample (~680 files) | `C:\Users\<you>\Pictures` | `/mnt/sample-pictures` |
| Full library (SMB share) | `\\192.168.2.21\pictures` (`P:\`) | `/mnt/pictures` (same path as on the server) |

Create the mount points and an SMB credentials file that only root can read:

```bash
sudo mkdir -p /mnt/sample-pictures /mnt/pictures

sudo tee /etc/smb-pictures.cred > /dev/null <<'EOF'
username=YOUR_SMB_USER
password=YOUR_SMB_PASSWORD
EOF
sudo chmod 600 /etc/smb-pictures.cred
```

Add both mounts to `/etc/fstab` (`sudo nano /etc/fstab`). Replace `<you>` with your Windows username. `\134` is how fstab escapes a backslash.

```fstab
C:\134Users\134<you>\134Pictures  /mnt/sample-pictures  drvfs  ro,noatime,uid=1000,gid=1000  0  0
//192.168.2.21/pictures  /mnt/pictures  cifs  ro,credentials=/etc/smb-pictures.cred,uid=1000,gid=1000,iocharset=utf8,noserverino,_netdev,nofail  0  0
```

Mount and verify:

```bash
sudo mount -a
ls /mnt/sample-pictures | head
ls /mnt/pictures        # 2006 2007 ... Workshop
touch /mnt/pictures/x   # must FAIL with "Read-only file system"
```

WSL reads `/etc/fstab` at startup. If the SMB share wasn't reachable then, which the `nofail` option tolerates, remount it with `sudo mount /mnt/pictures`.

### 6. Clone the repo inside WSL

Keep the repo on the Linux filesystem, not under `/mnt/c`. The `/mnt/c` bridge is slow for `node_modules` and test runs.

```bash
mkdir -p ~/src && cd ~/src
gh repo clone MusikPolice/photo-triage
cd photo-triage
```

### 7. Toolchain via mise 🚧

[mise](https://mise.jdx.dev/) installs the pinned versions of Python, uv, Node, pnpm, and just listed in `mise.toml`.

```bash
curl https://mise.run | sh
echo 'eval "$(~/.local/bin/mise activate bash)"' >> ~/.bashrc
exec bash

cd ~/src/photo-triage
mise trust && mise install
```

Then run the bootstrap script. It installs the pinned `exiftool` and `ffmpeg`, syncs Python and Node dependencies, installs the pre-commit hooks, and downloads model weights to `~/.cache/photo-triage/models`:

```bash
./scripts/bootstrap.sh
just doctor          # verifies versions, mounts, model weights
```

### 8. Local configuration 🚧

```bash
cp .env.example .env
```

The defaults point `PHOTO_DIR` at `/mnt/sample-pictures` and keep app state in `./data` and `./trash`. To test anything that **writes** to photos (trash, EXIF write-back), work on a throwaway copy:

```bash
just seed-scratch    # copies the sample to ~/photo-triage-data/scratch
# then set PHOTO_DIR=~/photo-triage-data/scratch in .env
```

### 9. Editor: VS Code with Remote-WSL

1. On Windows, install [VS Code](https://code.visualstudio.com/) and the **WSL** extension (`ms-vscode-remote.remote-wsl`).
2. From the repo in Ubuntu, run `code .`. VS Code opens on Windows, connected to WSL. Install the recommended extensions (Python, Pylance, Ruff, Svelte, ESLint, Prettier) *in WSL* when prompted.

### 10. Claude Code in WSL

Install Claude Code **inside Ubuntu**, not on Windows, so it sees the same filesystem, tools, and mounts as the build:

```bash
curl -fsSL https://claude.ai/install.sh | bash
cd ~/src/photo-triage
claude               # first run opens a browser to log in
```

If you use the VS Code extension, open the repo through Remote-WSL (step 9) and the extension runs on the WSL side automatically.

---

## Day-to-day commands 🚧

| Command | What it does |
|---|---|
| `just dev` | API + worker + Vite dev server (hot reload) against the sample library; Ollama in Docker |
| `just stack` | Production-like Docker Compose stack against the committed synthetic fixtures |
| `just test` | Unit, integration, and safety tests (fake ML models; fast) |
| `just test-models` | Tests that run the real CLIP/InsightFace models (slow; cached weights) |
| `just e2e` | Playwright end-to-end tests against `just stack` |
| `just check` | Everything CI runs: format, lint, pyright, import contracts, tests, coverage, frontend checks |
| `just dry-run-full` | Scale test against `/mnt/pictures` with metadata writes disabled |
| `just doctor` | Verify the environment |

See [docs/dev-environment.md](docs/dev-environment.md) for the full testing strategy and the checks CI enforces.

## Safety rules for contributors

- Never commit photos or videos. A pre-commit hook rejects media outside `backend/tests/fixtures/synthetic/`.
- Never mount the real libraries read-write in development.
- Only `photo_triage.files` may move, delete, or rewrite files. Lint rules enforce this.

## Troubleshooting

- **`/mnt/pictures` is empty after a reboot:** the share wasn't reachable when WSL started. Run `sudo mount /mnt/pictures`.
- **`mount error(13): Permission denied`:** check `/etc/smb-pictures.cred`. Some NAS setups also need `vers=3.0` added to the fstab options.
- **`docker: command not found` in WSL:** enable WSL integration for Ubuntu-24.04 in Docker Desktop, then restart the terminal.
- **Slow installs or tests:** make sure the repo is under `~/src`, not `/mnt/c/...`.
