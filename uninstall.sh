#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# lmux uninstaller — removes all lmux components
# Usage: bash ~/.lmux/uninstall.sh
# ──────────────────────────────────────────────────────────────────────────────
set -e

LMUX_INSTALL_DIR="${LMUX_INSTALL_DIR:-$HOME/.lmux}"

echo ""
echo "  ██╗     ███╗   ███╗██╗   ██╗██╗  ██╗"
echo "  ██║     ████╗ ████║██║   ██║╚██╗██╔╝"
echo "  ██║     ██╔████╔██║██║   ██║ ╚███╔╝ "
echo "  ██║     ██║╚██╔╝██║██║   ██║ ██╔██╗ "
echo "  ███████╗██║ ╚═╝ ██║╚██████╔╝██╔╝ ██╗"
echo "  ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝"
echo "  Language Model Multiplexer — Uninstaller"
echo ""

# ── Confirmation ──────────────────────────────────────────────────────────────
echo "  This will remove:"
echo "    • Docker containers (lmux-engine, lmux-ui)"
echo "    • Docker volumes (lmux-webui-data)"
echo "    • Python virtual environment ($LMUX_INSTALL_DIR/.venv)"
echo "    • Shell alias from .bashrc / .zshrc / PowerShell profile"
echo "    • lmux installation directory ($LMUX_INSTALL_DIR)"
echo ""

# Check if models should be kept
KEEP_MODELS=true
if [ -f "$LMUX_INSTALL_DIR/registry.json" ]; then
  MODELS_PATH=$(python3 -c "import json; r=json.load(open('$LMUX_INSTALL_DIR/registry.json')); print(r.get('models_path',''))" 2>/dev/null || echo "")
fi

echo "  Downloaded GGUF model files will NOT be deleted by default."
echo "  Use --delete-models to also remove all downloaded models."
echo ""

DELETE_MODELS=false
for arg in "$@"; do
  case "$arg" in
    --delete-models) DELETE_MODELS=true ;;
    --yes|-y)        SKIP_CONFIRM=true ;;
  esac
done

if [ "$SKIP_CONFIRM" != "true" ]; then
  read -p "  Continue? [y/N] " confirm
  if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "  Cancelled."
    exit 0
  fi
fi

echo ""

# ── Stop and remove Docker containers ────────────────────────────────────────
echo "  → Stopping Docker containers..."
if command -v docker > /dev/null 2>&1; then
  docker compose -f "$LMUX_INSTALL_DIR/docker-compose.yml" down 2>/dev/null || true
  docker rm -f lmux-engine lmux-ui 2>/dev/null || true
  echo "  ✓  Containers removed"

  # Remove Docker volume
  docker volume rm lmux-webui-data 2>/dev/null || true
  echo "  ✓  Docker volumes removed"
else
  echo "  !  Docker not found, skipping container cleanup"
fi

# ── Remove shell alias ──────────────────────────────────────────────────────
echo "  → Removing shell aliases..."
OS_TYPE="$(uname -s 2>/dev/null || echo "Windows")"

case "$OS_TYPE" in
  Linux*)   OS="linux" ;;
  Darwin*)  OS="mac" ;;
  *)        OS="linux" ;;
esac

for profile_file in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.config/fish/config.fish"; do
  if [ -f "$profile_file" ]; then
    if grep -q "alias lmux=" "$profile_file" 2>/dev/null || grep -q "# lmux —" "$profile_file" 2>/dev/null; then
      if [ "$OS" = "mac" ]; then
        sed -i '' '/# lmux —/d' "$profile_file"
        sed -i '' '/alias lmux=/d' "$profile_file"
      else
        sed -i '/# lmux —/d' "$profile_file"
        sed -i '/alias lmux=/d' "$profile_file"
      fi
      echo "  ✓  Alias removed from $profile_file"
    fi
  fi
done

# Windows PowerShell profile
if [ "$OS_TYPE" = "CYGWIN" ] || [ "$OS_TYPE" = "MINGW" ] || [ "$OS_TYPE" = "MSYS" ]; then
  PS_PROFILE=$(powershell.exe -Command 'echo $PROFILE' 2>/dev/null | tr -d '\r' || echo "")
  if [ -n "$PS_PROFILE" ] && [ -f "$PS_PROFILE" ]; then
    if grep -q "function lmux" "$PS_PROFILE" 2>/dev/null; then
      sed -i '/# lmux —/d' "$PS_PROFILE"
      sed -i '/function lmux/d' "$PS_PROFILE"
      echo "  ✓  PowerShell alias removed"
    fi
  fi
fi

# ── Remove virtual environment ──────────────────────────────────────────────
if [ -d "$LMUX_INSTALL_DIR/.venv" ]; then
  echo "  → Removing virtual environment..."
  rm -rf "$LMUX_INSTALL_DIR/.venv"
  echo "  ✓  Virtual environment removed"
fi

# ── Remove models (if requested) ────────────────────────────────────────────
if [ "$DELETE_MODELS" = "true" ]; then
  echo "  → Removing downloaded models..."
  # Remove default models dir
  if [ -d "$LMUX_INSTALL_DIR/models" ]; then
    rm -rf "$LMUX_INSTALL_DIR/models"
    echo "  ✓  Removed $LMUX_INSTALL_DIR/models"
  fi
  # Remove custom models path if different
  if [ -n "$MODELS_PATH" ] && [ -d "$MODELS_PATH" ]; then
    rm -rf "$MODELS_PATH"
    echo "  ✓  Removed custom models: $MODELS_PATH"
  fi
else
  echo "  →  Models kept on disk. Remove manually if needed:"
  if [ -d "$LMUX_INSTALL_DIR/models" ]; then
    MODEL_SIZE=$(du -sh "$LMUX_INSTALL_DIR/models" 2>/dev/null | awk '{print $1}')
    echo "     $LMUX_INSTALL_DIR/models ($MODEL_SIZE)"
  fi
  if [ -n "$MODELS_PATH" ] && [ -d "$MODELS_PATH" ]; then
    MODEL_SIZE=$(du -sh "$MODELS_PATH" 2>/dev/null | awk '{print $1}')
    echo "     $MODELS_PATH ($MODEL_SIZE)"
  fi
fi

# ── Remove lmux installation directory ──────────────────────────────────────
echo "  → Removing lmux installation..."
rm -rf "$LMUX_INSTALL_DIR"
echo "  ✓  $LMUX_INSTALL_DIR removed"

echo ""
echo "  ┌───────────────────────────────────────────────┐"
echo "  │        lmux uninstalled successfully          │"
echo "  │                                                │"
echo "  │  Restart your shell or run:                    │"
echo "  │  source ~/.bashrc  (or ~/.zshrc)               │"
echo "  └───────────────────────────────────────────────┘"
echo ""
