#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# lmux installer — cross-platform (Linux / macOS / Windows WSL)
# Usage: curl -sSL https://raw.githubusercontent.com/rohan-aistudio/lmux/main/install.sh | sh
#        or:  bash install.sh
# ──────────────────────────────────────────────────────────────────────────────
set -e

LMUX_INSTALL_DIR="${LMUX_INSTALL_DIR:-$HOME/.lmux}"
LMUX_REPO="${LMUX_REPO:-https://github.com/rohan-aistudio/lmux.git}"
LMUX_VENV="$LMUX_INSTALL_DIR/.venv"

echo ""
echo "  ██╗     ███╗   ███╗██╗   ██╗██╗  ██╗"
echo "  ██║     ████╗ ████║██║   ██║╚██╗██╔╝"
echo "  ██║     ██╔████╔██║██║   ██║ ╚███╔╝ "
echo "  ██║     ██║╚██╔╝██║██║   ██║ ██╔██╗ "
echo "  ███████╗██║ ╚═╝ ██║╚██████╔╝██╔╝ ██╗"
echo "  ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝"
echo "  Language Model Multiplexer — Installer"
echo ""

# ── Detect OS ────────────────────────────────────────────────────────────────
OS_TYPE="$(uname -s 2>/dev/null || echo "Windows")"
ARCH="$(uname -m 2>/dev/null || echo "x86_64")"

case "$OS_TYPE" in
  Linux*)   OS="linux" ;;
  Darwin*)  OS="mac" ;;
  CYGWIN*|MINGW*|MSYS*) OS="windows" ;;
  *)        OS="linux" ;;
esac

echo "  → Detected OS: $OS ($ARCH)"

# ── Check Git ────────────────────────────────────────────────────────────────
if ! command -v git > /dev/null 2>&1; then
  echo "  ✗  git not found."
  if [ "$OS" = "linux" ]; then
    echo "     Run: sudo apt install git"
  elif [ "$OS" = "mac" ]; then
    echo "     Run: xcode-select --install  (or brew install git)"
  fi
  exit 1
fi
echo "  ✓  Git: $(git --version | head -1)"

# ── Check / Install uv ──────────────────────────────────────────────────────
if ! command -v uv > /dev/null 2>&1; then
  echo "  →  uv not found. Installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Source the env so uv is available in this session
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv > /dev/null 2>&1; then
    echo "  ✗  uv installation failed."
    echo "     Install manually: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi
echo "  ✓  uv: $(uv --version)"

# ── Check Docker ─────────────────────────────────────────────────────────────
if ! command -v docker > /dev/null 2>&1; then
  echo "  ✗  Docker not found."
  echo "     Install from: https://docs.docker.com/get-docker/"
  exit 1
fi
echo "  ✓  Docker: $(docker --version | head -1)"

# ── Check NVIDIA GPU ─────────────────────────────────────────────────────────
GPU_AVAILABLE=false
if command -v nvidia-smi > /dev/null 2>&1; then
  GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1 || echo "")
  if [ -n "$GPU_INFO" ]; then
    GPU_AVAILABLE=true
    echo "  ✓  GPU: $GPU_INFO"

    # ── Install NVIDIA Container Toolkit if missing ────────────────────────
    if ! command -v nvidia-container-toolkit > /dev/null 2>&1 && ! dpkg -l nvidia-container-toolkit > /dev/null 2>&1; then
      echo ""
      echo "  → NVIDIA GPU detected but nvidia-container-toolkit not found."
      echo "  → Attempting to install NVIDIA Container Toolkit..."
      if [ "$OS" = "linux" ]; then
        if command -v apt-get > /dev/null 2>&1; then
          distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
          curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null || true
          curl -s -L "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list" | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null
          sudo apt-get update -qq
          sudo apt-get install -y -qq nvidia-container-toolkit
          sudo nvidia-ctk runtime configure --runtime=docker
          sudo systemctl restart docker
          echo "  ✓  NVIDIA Container Toolkit installed"
        else
          echo "  !  Non-apt system — install manually:"
          echo "     https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        fi
      fi
    else
      echo "  ✓  NVIDIA Container Toolkit available"
    fi
  fi
fi

# ── Apple Silicon Metal check ────────────────────────────────────────────────
if [ "$OS" = "mac" ] && [ "$ARCH" = "arm64" ]; then
  echo "  ✓  Apple Silicon (Metal) detected"
fi

# ── AMD ROCm check ───────────────────────────────────────────────────────────
if command -v rocminfo > /dev/null 2>&1; then
  echo "  ✓  AMD ROCm detected"
fi

if [ "$GPU_AVAILABLE" = "false" ] && [ "$OS" != "mac" ] || { [ "$OS" = "mac" ] && [ "$ARCH" != "arm64" ]; }; then
  echo "  !  No GPU detected — will use CPU+RAM mode"
fi

# ── Clone / update repo ──────────────────────────────────────────────────────
if [ -d "$LMUX_INSTALL_DIR/.git" ]; then
  echo "  → Updating existing installation..."
  git -C "$LMUX_INSTALL_DIR" pull --quiet
  echo "  ✓  Updated to latest version"
else
  echo "  → Cloning lmux to $LMUX_INSTALL_DIR..."
  git clone --quiet "$LMUX_REPO" "$LMUX_INSTALL_DIR"
  echo "  ✓  Cloned lmux"
fi

LMUX_PY="$LMUX_INSTALL_DIR/lmux.py"

# ── Create isolated venv using uv ────────────────────────────────────────────
echo ""
echo "  → Creating isolated Python environment (python ≥3.12)..."
if [ ! -d "$LMUX_VENV" ]; then
  uv venv "$LMUX_VENV" --python ">=3.12"
fi
echo "  ✓  Virtual environment: $LMUX_VENV"

# Activate venv for this session
LMUX_PYTHON="$LMUX_VENV/bin/python"

# ── Install Python dependencies (isolated in venv) ───────────────────────────
echo "  → Installing Python dependencies in venv..."
uv pip install --python "$LMUX_PYTHON" huggingface_hub hf-transfer pyyaml
echo "  ✓  huggingface_hub, pyyaml installed (isolated)"

# ── Set up shell alias (points at venv python) ───────────────────────────────
SHELL_NAME="$(basename "${SHELL:-bash}")"
ALIAS_LINE="alias lmux='$LMUX_PYTHON $LMUX_PY'"

if [ "$OS" = "linux" ] || [ "$OS" = "mac" ]; then
  if [ "$SHELL_NAME" = "zsh" ]; then
    PROFILE="$HOME/.zshrc"
  elif [ "$SHELL_NAME" = "fish" ]; then
    PROFILE="$HOME/.config/fish/config.fish"
    ALIAS_LINE="alias lmux '$LMUX_PYTHON $LMUX_PY'"
  else
    PROFILE="$HOME/.bashrc"
  fi

  # Remove old alias if present (in case python path changed)
  if grep -q "alias lmux=" "$PROFILE" 2>/dev/null; then
    # Update existing alias
    if [ "$OS" = "mac" ]; then
      sed -i '' "/alias lmux=/d" "$PROFILE"
    else
      sed -i "/alias lmux=/d" "$PROFILE"
    fi
  fi

  echo "" >> "$PROFILE"
  echo "# lmux — Language Model Multiplexer" >> "$PROFILE"
  echo "$ALIAS_LINE" >> "$PROFILE"
  echo "  ✓  Alias added to $PROFILE"
  echo "  →  Run: source $PROFILE  (or open a new terminal)"
fi

# ── Windows PowerShell alias (WSL passthrough) ────────────────────────────────
if [ "$OS" = "windows" ]; then
  PS_PROFILE=$(powershell.exe -Command 'echo $PROFILE' 2>/dev/null | tr -d '\r' || echo "")
  if [ -n "$PS_PROFILE" ]; then
    PS_FUNC="function lmux { & '$LMUX_PYTHON' '$LMUX_PY' \$args }"
    if ! grep -q "function lmux" "$PS_PROFILE" 2>/dev/null; then
      echo "$PS_FUNC" >> "$PS_PROFILE"
      echo "  ✓  PowerShell alias added"
    fi
  fi
fi

# ── Create models directory ───────────────────────────────────────────────────
mkdir -p "$LMUX_INSTALL_DIR/models"
echo "  ✓  models/ directory ready"

# ── Run lmux init (GPU detection + compose generation + docker up) ─────────────
echo ""
echo "  → Initializing lmux stack..."
"$LMUX_PYTHON" "$LMUX_PY" init

echo ""
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │              lmux installed successfully             │"
echo "  │                                                      │"
echo "  │  Start pulling models:                               │"
echo "  │  lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF  │"
echo "  │         --quant Q4_K_M                               │"
echo "  │                                                      │"
echo "  │  Uninstall:                                          │"
echo "  │  bash ~/.lmux/uninstall.sh                           │"
echo "  └─────────────────────────────────────────────────────┘"
echo ""
