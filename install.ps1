# install.ps1 — lmux installer for Windows (PowerShell)
# Run as: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$LMUX_INSTALL_DIR = if ($env:LMUX_INSTALL_DIR) { $env:LMUX_INSTALL_DIR } else { "$env:USERPROFILE\.lmux" }
$LMUX_REPO = if ($env:LMUX_REPO) { $env:LMUX_REPO } else { "https://github.com/rohan-aistudio/lmux.git" }
$LMUX_VENV = "$LMUX_INSTALL_DIR\.venv"

Write-Host ""
Write-Host "  ██╗     ███╗   ███╗██╗   ██╗██╗  ██╗" -ForegroundColor Cyan
Write-Host "  ██║     ████╗ ████║██║   ██║╚██╗██╔╝" -ForegroundColor Cyan
Write-Host "  ██║     ██╔████╔██║██║   ██║ ╚███╔╝ " -ForegroundColor Cyan
Write-Host "  ██║     ██║╚██╔╝██║██║   ██║ ██╔██╗ " -ForegroundColor Cyan
Write-Host "  ███████╗██║ ╚═╝ ██║╚██████╔╝██╔╝ ██╗" -ForegroundColor Cyan
Write-Host "  ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝" -ForegroundColor Cyan
Write-Host "  Language Model Multiplexer — Windows Installer"
Write-Host ""

# ── Check Git ─────────────────────────────────────────────────────────────────
try {
    $gitVer = git --version
    Write-Host "  ✓  Git: $gitVer" -ForegroundColor Green
} catch {
    Write-Host "  ✗  Git not found. Install from: https://git-scm.com/download/win" -ForegroundColor Red
    exit 1
}

# ── Check / Install uv ───────────────────────────────────────────────────────
try {
    $uvVer = uv --version 2>$null
    Write-Host "  ✓  uv: $uvVer" -ForegroundColor Green
} catch {
    Write-Host "  →  uv not found. Installing..." -ForegroundColor Cyan
    irm https://astral.sh/uv/install.ps1 | iex
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    try {
        $uvVer = uv --version
        Write-Host "  ✓  uv: $uvVer" -ForegroundColor Green
    } catch {
        Write-Host "  ✗  uv installation failed. Install manually: https://docs.astral.sh/uv/" -ForegroundColor Red
        exit 1
    }
}

# ── Check Docker ──────────────────────────────────────────────────────────────
try {
    $dockerVer = docker --version
    Write-Host "  ✓  Docker: $dockerVer" -ForegroundColor Green
} catch {
    Write-Host "  ✗  Docker not found. Install from: https://docs.docker.com/desktop/windows/" -ForegroundColor Red
    exit 1
}

# ── Check NVIDIA GPU ──────────────────────────────────────────────────────────
try {
    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
    if ($gpuInfo) {
        Write-Host "  ✓  GPU: $($gpuInfo.Trim())" -ForegroundColor Green
        try {
            $nctk = docker info 2>$null | Select-String "nvidia"
            if (-not $nctk) {
                Write-Host "  !  NVIDIA Container Toolkit may not be configured for Docker." -ForegroundColor Yellow
                Write-Host "     Install from: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
            }
        } catch {}
    }
} catch {
    Write-Host "  !  No NVIDIA GPU detected — will use CPU+RAM mode" -ForegroundColor Yellow
}

# ── Clone / update repo ──────────────────────────────────────────────────────
if (Test-Path "$LMUX_INSTALL_DIR\.git") {
    Write-Host "  → Updating existing installation..."
    git -C $LMUX_INSTALL_DIR pull --quiet
    Write-Host "  ✓  Updated to latest version" -ForegroundColor Green
} else {
    Write-Host "  → Cloning lmux to $LMUX_INSTALL_DIR..."
    git clone --quiet $LMUX_REPO $LMUX_INSTALL_DIR
    Write-Host "  ✓  Cloned lmux" -ForegroundColor Green
}

$LMUX_PY = Join-Path $LMUX_INSTALL_DIR "lmux.py"

# ── Create isolated venv using uv ────────────────────────────────────────────
Write-Host ""
Write-Host "  → Creating isolated Python environment (python ≥3.12)..."
if (-not (Test-Path $LMUX_VENV)) {
    uv venv $LMUX_VENV --python ">=3.12"
}
Write-Host "  ✓  Virtual environment: $LMUX_VENV" -ForegroundColor Green

$LMUX_PYTHON = Join-Path $LMUX_VENV "Scripts\python.exe"

# ── Install Python dependencies (isolated in venv) ───────────────────────────
Write-Host "  → Installing Python dependencies in venv..."
uv pip install --python $LMUX_PYTHON huggingface_hub pyyaml
Write-Host "  ✓  huggingface_hub, pyyaml installed (isolated)" -ForegroundColor Green

# ── Add PowerShell function ───────────────────────────────────────────────────
$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Path $profileDir | Out-Null }
if (-not (Test-Path $PROFILE))    { New-Item -ItemType File      -Path $PROFILE    | Out-Null }

# Remove old alias if present
$existingProfile = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($existingProfile -match "function lmux") {
    $lines = Get-Content $PROFILE | Where-Object { $_ -notmatch "function lmux" -and $_ -notmatch "# lmux —" }
    $lines | Set-Content $PROFILE
}

Add-Content $PROFILE "`n# lmux — Language Model Multiplexer"
Add-Content $PROFILE "function lmux { & `"$LMUX_PYTHON`" `"$LMUX_PY`" @args }"
Write-Host "  ✓  PowerShell alias added to $PROFILE" -ForegroundColor Green
Write-Host "  →  Run: . `$PROFILE  (to activate in current session)" -ForegroundColor Cyan

# ── Create models directory ───────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path (Join-Path $LMUX_INSTALL_DIR "models") | Out-Null
Write-Host "  ✓  models\ directory ready" -ForegroundColor Green

# ── Run lmux init ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  → Initializing lmux stack..."
& $LMUX_PYTHON $LMUX_PY init

Write-Host ""
Write-Host "  lmux installed. Pull your first model:" -ForegroundColor Green
Write-Host "  lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Uninstall: powershell -File $LMUX_INSTALL_DIR\uninstall.ps1" -ForegroundColor DarkGray
Write-Host ""
