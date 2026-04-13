# install.ps1 — lmux installer for Windows (PowerShell)
# Run as: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$LMUX_INSTALL_DIR = if ($env:LMUX_INSTALL_DIR) { $env:LMUX_INSTALL_DIR } else { "$env:USERPROFILE\.lmux" }
$LMUX_REPO = if ($env:LMUX_REPO) { $env:LMUX_REPO } else { "https://github.com/your-user/lmux.git" }

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

# ── Check Python ──────────────────────────────────────────────────────────────
$PYTHON = $null
foreach ($cmd in @("python", "python3")) {
    try {
        $ver = & $cmd -c "import sys; print(sys.version_info >= (3,10))" 2>$null
        if ($ver -eq "True") { $PYTHON = $cmd; break }
    } catch {}
}
if (-not $PYTHON) {
    Write-Host "  ✗  Python 3.10+ not found." -ForegroundColor Red
    Write-Host "     Download from: https://python.org/downloads/"
    exit 1
}
Write-Host "  ✓  Python: $(& $PYTHON --version)" -ForegroundColor Green

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

        # Check for NVIDIA Container Toolkit (Docker GPU support)
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

# ── Install Python dependencies ───────────────────────────────────────────────
Write-Host ""
Write-Host "  → Installing Python dependencies..."
& $PYTHON -m pip install --quiet --upgrade huggingface_hub pyyaml
Write-Host "  ✓  huggingface_hub, pyyaml installed" -ForegroundColor Green

# ── Add PowerShell function ───────────────────────────────────────────────────
$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Path $profileDir | Out-Null }
if (-not (Test-Path $PROFILE))    { New-Item -ItemType File      -Path $PROFILE    | Out-Null }

$existingProfile = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($existingProfile -notmatch "function lmux") {
    Add-Content $PROFILE "`n# lmux — Language Model Multiplexer"
    Add-Content $PROFILE "function lmux { python `"$LMUX_PY`" @args }"
    Write-Host "  ✓  PowerShell alias added to $PROFILE" -ForegroundColor Green
    Write-Host "  →  Run: . `$PROFILE  (to activate in current session)" -ForegroundColor Cyan
} else {
    Write-Host "  ✓  Alias already in $PROFILE" -ForegroundColor Green
}

# ── Create models directory ───────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path (Join-Path $LMUX_INSTALL_DIR "models") | Out-Null
Write-Host "  ✓  models\ directory ready" -ForegroundColor Green

# ── Run lmux init ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  → Initializing lmux stack..."
& $PYTHON $LMUX_PY init

Write-Host ""
Write-Host "  lmux installed. Pull your first model:" -ForegroundColor Green
Write-Host "  lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M" -ForegroundColor Cyan
Write-Host ""
