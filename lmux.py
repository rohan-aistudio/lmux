#!/usr/bin/env python3
"""
lmux — Language Model Multiplexer
Ollama-style CLI for dynamic LLM loading via llama-swap + Open WebUI

Commands:
  lmux init              Detect GPU/OS and scaffold the stack
  lmux pull <src>        Download a GGUF from HuggingFace
  lmux ls                List registered models
  lmux rm  <name>        Remove a model
  lmux info <name>       Show model metadata + VRAM estimate
  lmux status            Show live loaded models + VRAM
  lmux stats             Token throughput from last session
  lmux run  <name> <p>   One-shot CLI inference (ollama-style)
  lmux reload            Resync config and restart llama-swap
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import textwrap
from pathlib import Path
from urllib.parse import urlparse

# ── PROJECT PATHS ────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent.resolve()
MODELS_DIR    = BASE_DIR / "models"
CONFIG_FILE   = BASE_DIR / "config.yaml"
REGISTRY_FILE = BASE_DIR / "registry.json"
COMPOSE_FILE  = BASE_DIR / "docker-compose.yml"
ENV_FILE      = BASE_DIR / ".env"

# ── PORT SCHEME ──────────────────────────────────────────────────────────────
API_PORT      = 11435   # OpenAI-compatible endpoint  →  http://localhost:11435/v1
WEBUI_PORT    = 11436   # Open WebUI                  →  http://localhost:11436
MODEL_PORT_START = 12000  # Internal per-model ports  (12000, 12001, …)

# ── VRAM BUDGET ──────────────────────────────────────────────────────────────
VRAM_BUDGET_GB = 7.5    # 8GB card, 0.5GB headroom
MAX_RUNNING    = 1      # concurrent loaded models

# Bits-per-weight lookup for VRAM estimation
QUANT_BPW = {
    "IQ1_S": 1.56, "IQ1_M": 1.75,
    "IQ2_XXS": 2.06, "IQ2_XS": 2.31, "IQ2_S": 2.50, "IQ2_M": 2.70,
    "Q2_K": 2.63,
    "IQ3_XXS": 3.06, "IQ3_XS": 3.3, "Q3_K_S": 3.0, "Q3_K_M": 3.35, "Q3_K_L": 3.56,
    "Q4_0": 4.55, "Q4_K_S": 4.37, "Q4_K_M": 4.85, "Q4_K_L": 4.90,
    "IQ4_NL": 4.50, "IQ4_XS": 4.25,
    "Q5_0": 5.54, "Q5_K_S": 5.54, "Q5_K_M": 5.68,
    "Q6_K": 6.59, "Q8_0": 8.50, "F16": 16.0, "F32": 32.0,
}

# ── COLORS / UX ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"

def ok(msg):    print(f"  {GREEN}✓{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}!{RESET}  {msg}")
def err(msg):   print(f"  {RED}✗{RESET}  {msg}")
def info(msg):  print(f"  {CYAN}→{RESET}  {msg}")
def head(msg):  print(f"\n{BOLD}{msg}{RESET}")
def die(msg):   err(msg); sys.exit(1)

def banner():
    print(f"""
{BOLD}{CYAN}  ██╗     ███╗   ███╗██╗   ██╗██╗  ██╗
  ██║     ████╗ ████║██║   ██║╚██╗██╔╝
  ██║     ██╔████╔██║██║   ██║ ╚███╔╝
  ██║     ██║╚██╔╝██║██║   ██║ ██╔██╗
  ███████╗██║ ╚═╝ ██║╚██████╔╝██╔╝ ██╗
  ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝{RESET}
  {DIM}Language Model Multiplexer{RESET}
""")


# ── ENV / TOKEN ──────────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def save_env(env: dict):
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def get_hf_token() -> str:
    # Priority: env var → .env file → prompt user
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        env = load_env()
        token = env.get("HF_TOKEN", "")
    if not token:
        print()
        info("HuggingFace token not found.")
        info("Get yours at: https://huggingface.co/settings/tokens")
        token = input(f"  {CYAN}  Token:{RESET} ").strip()
        if not token:
            die("No token provided. Private models and gated repos require a token.")
        env = load_env()
        env["HF_TOKEN"] = token
        save_env(env)
        ok("Token saved to .env")
    return token


# ── GPU / PLATFORM DETECTION ─────────────────────────────────────────────────

def detect_platform() -> dict:
    system = platform.system()
    arch   = platform.machine()
    result = {"os": system, "arch": arch, "gpu": None, "gpu_name": "", "vram_gb": 0, "backend": "cpu"}

    # ── NVIDIA ──
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            out = subprocess.run(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if out.returncode == 0 and out.stdout.strip():
                line = out.stdout.strip().splitlines()[0]
                parts = line.split(",")
                gpu_name = parts[0].strip()
                vram_str = parts[1].strip().lower().replace("mib", "").strip()
                vram_gb  = round(int(vram_str) / 1024, 1)
                result.update({
                    "gpu": "nvidia", "gpu_name": gpu_name,
                    "vram_gb": vram_gb, "backend": "cuda"
                })
                return result
        except Exception:
            pass

    # ── Apple Silicon ──
    if system == "Darwin" and arch == "arm64":
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=5
            )
            result.update({"gpu": "metal", "gpu_name": "Apple Silicon (Metal)", "backend": "metal"})
        except Exception:
            pass
        return result

    # ── AMD ROCm ──
    rocm = shutil.which("rocminfo")
    if rocm:
        result.update({"gpu": "amd", "gpu_name": "AMD GPU (ROCm)", "backend": "rocm"})

    return result


def force_cpu_mode() -> bool:
    """Check if user forced CPU mode via CUDA=0 or --cpu flag."""
    return os.environ.get("CUDA", "1") == "0"


# ── REGISTRY ─────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {"models": {}, "next_port": MODEL_PORT_START, "platform": {}}


def save_registry(reg: dict):
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


def alloc_port(reg: dict) -> int:
    p = reg["next_port"]
    reg["next_port"] = p + 1
    return p


# ── VRAM ESTIMATION ──────────────────────────────────────────────────────────

def estimate_vram(filename: str, size_bytes: int) -> tuple[float, str]:
    quant = "Q4_K_M"
    for q in sorted(QUANT_BPW, key=len, reverse=True):
        if q.upper() in filename.upper():
            quant = q
            break
    bpw     = QUANT_BPW.get(quant, 4.85)
    # Estimate param count from file size
    # GGUF header overhead ~100MB
    data_bytes = max(size_bytes - 100 * 1024 * 1024, size_bytes * 0.95)
    params_b   = data_bytes / (bpw / 8)
    vram_gb    = (params_b * bpw / 8 / 1e9) * 1.12  # 12% activation overhead
    return round(vram_gb, 2), quant


# ── DOCKER-COMPOSE GENERATION ─────────────────────────────────────────────────

def write_compose(plat: dict, cpu_override: bool = False):
    use_gpu = plat["backend"] in ("cuda", "rocm", "metal") and not cpu_override

    gpu_block = ""
    if use_gpu and plat["backend"] == "cuda":
        gpu_block = """    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
"""

    compose = f"""# Auto-generated by lmux — do not edit manually
# Regenerate with: lmux init
# API endpoint  → http://localhost:{API_PORT}/v1
# Open WebUI    → http://localhost:{WEBUI_PORT}

version: "3.8"

networks:
  lmux-net:
    driver: bridge

services:
  llama-swap:
    image: ghcr.io/mostlygeek/llama-swap:{"cuda" if use_gpu and plat["backend"] == "cuda" else "latest"}
    container_name: lmux-engine
    networks:
      - lmux-net
    ports:
      - "{API_PORT}:8080"
    volumes:
      - ./models:/models
      - ./config.yaml:/app/config.yaml
{gpu_block}    restart: unless-stopped
    environment:
      # Unified memory: overflow VRAM into system RAM (32GB available)
      - GGML_CUDA_ENABLE_UNIFIED_MEMORY=1
      # Flash attention: saves ~15% VRAM on Ampere/Ada
      - LLAMA_FLASH_ATTN=1

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: lmux-ui
    networks:
      - lmux-net
    ports:
      - "{WEBUI_PORT}:8080"
    volumes:
      - lmux-webui-data:/app/backend/data
    depends_on:
      - llama-swap
    restart: unless-stopped
    environment:
      # Point at lmux engine (internal Docker DNS)
      - OPENAI_API_BASE_URL=http://llama-swap:8080/v1
      - OPENAI_API_KEY=lmux-local
      # Enables green dot "active" status + model monitor in Open WebUI
      - ENABLE_OLLAMA_API=true
      - OLLAMA_BASE_URL=http://llama-swap:8080
      # Disable features that add idle load
      - ENABLE_RAG_WEB_SEARCH=false
      - ENABLE_IMAGE_GENERATION=false
      - WEBUI_AUTH=false

volumes:
  lmux-webui-data:
"""
    COMPOSE_FILE.write_text(compose)


# ── CONFIG.YAML GENERATION ────────────────────────────────────────────────────

def write_config(reg: dict, cpu_override: bool = False):
    n_gpu = 0 if cpu_override else 99
    models_block = ""

    for name, m in reg["models"].items():
        port  = m["port"]
        gguf  = m["gguf_filename"]
        vram  = m.get("vram_estimate_gb", "?")
        quant = m.get("quant", "")
        models_block += f"""
  # {name}  |  {quant}  |  ~{vram}GB VRAM
  {name}:
    proxy: "http://127.0.0.1:{port}"
    cmd: >
      llama-server
        --model /models/{gguf}
        --port {port}
        --ctx-size 2048
        --n-gpu-layers {n_gpu}
        --flash-attn
        --metrics
"""

    if not models_block:
        models_block = "\n  # No models yet — run: lmux pull <url>\n"

    config = f"""# Auto-generated by lmux — do not edit manually
# Regenerate with: lmux reload

# Max models loaded concurrently (VRAM budget: {VRAM_BUDGET_GB}GB)
max_running: {MAX_RUNNING}

defaults:
  args:
    - --ctx-size
    - "2048"
    - --n-gpu-layers
    - "{n_gpu}"
    - --flash-attn
    - --metrics          # exposes /metrics for token stats
  # Unload model after 5min idle → frees VRAM for next tab
  ttl: 300

models:{models_block}
"""
    CONFIG_FILE.write_text(config)
    ok(f"config.yaml updated ({len(reg['models'])} models)")


# ── HUGGINGFACE HELPERS ───────────────────────────────────────────────────────

def parse_hf_url(url: str) -> tuple[str, str]:
    """
    Handles:
      https://huggingface.co/owner/repo/blob/main/file.gguf
      https://huggingface.co/owner/repo/resolve/main/file.gguf
    """
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 5 or parts[2] not in ("blob", "resolve"):
        raise ValueError(
            "Expected a HuggingFace file URL.\n"
            "  e.g. https://huggingface.co/bartowski/Meta-Llama-3-8B-Instruct-GGUF"
            "/blob/main/Meta-Llama-3-8B-Instruct-Q4_K_M.gguf\n\n"
            "  Or use shorthand: lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M"
        )
    repo_id  = f"{parts[0]}/{parts[1]}"
    filename = "/".join(parts[4:])
    return repo_id, filename


def pick_gguf(repo_id: str, quant_filter: str | None) -> str:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        die("Run: pip install huggingface_hub pyyaml")

    info(f"Scanning {repo_id} for GGUF files...")
    api   = HfApi()
    files = [f for f in api.list_repo_files(repo_id) if f.endswith(".gguf")]

    if not files:
        die(f"No GGUF files found in {repo_id}")

    if quant_filter:
        filtered = [f for f in files if quant_filter.upper() in f.upper()]
        if not filtered:
            warn(f"No GGUF matched quant '{quant_filter}'. Available:")
            for f in files:
                print(f"    {f}")
            sys.exit(1)
        if len(filtered) == 1:
            return filtered[0]
        files = filtered

    if len(files) == 1:
        return files[0]

    # Let user pick
    print()
    info(f"Multiple GGUFs found in {repo_id}:")
    for i, f in enumerate(files):
        print(f"    [{CYAN}{i}{RESET}] {f}")
    try:
        idx = int(input(f"\n  {CYAN}Select index:{RESET} ").strip())
        return files[idx]
    except (ValueError, IndexError):
        die("Invalid selection.")


def download_gguf(repo_id: str, filename: str, token: str) -> Path:
    try:
        from huggingface_hub import hf_hub_download, login
    except ImportError:
        die("Run: pip install huggingface_hub pyyaml")

    if token:
        login(token=token, add_to_git_credential=False)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    info(f"Downloading {filename}")
    info(f"Repo:  {repo_id}")
    info(f"Dest:  {MODELS_DIR}\n")

    local = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(MODELS_DIR),
    )
    return Path(local)


# ── DOCKER CONTROL ────────────────────────────────────────────────────────────

def stack_running() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format={{.State.Running}}", "lmux-engine"],
        capture_output=True, text=True
    )
    return result.returncode == 0 and "true" in result.stdout


def reload_engine():
    info("Reloading lmux engine (Open WebUI stays alive)...")
    result = subprocess.run(
        ["docker", "restart", "lmux-engine"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        err("docker restart failed:")
        print(f"     {result.stderr.strip()}")
        info("Is the stack running? Try: lmux start")
        return False

    print(f"  {CYAN}  Waiting for engine", end="", flush=True)
    import urllib.request
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://localhost:{API_PORT}/v1/models", timeout=1)
            print(f"  {GREEN}ready{RESET}")
            return True
        except Exception:
            print(".", end="", flush=True)

    print(f"\n  {YELLOW}Engine restarted but health check timed out.{RESET}")
    info("Check logs: docker logs lmux-engine")
    return False


# ── COMMANDS ─────────────────────────────────────────────────────────────────

def cmd_init(args):
    banner()
    head("Initializing Lmux")

    cpu_override = force_cpu_mode() or getattr(args, "cpu", False)

    # Detect platform
    plat = detect_platform()
    info(f"OS:  {plat['os']} ({plat['arch']})")

    if cpu_override:
        warn("CPU mode forced (CUDA=0 or --cpu). GPU will not be used.")
        plat["backend"] = "cpu"
    elif plat["gpu"]:
        ok(f"GPU: {plat['gpu_name']}  [{plat['backend'].upper()}]  {plat['vram_gb']}GB VRAM")
    else:
        warn("No GPU detected. Running in CPU+RAM mode.")

    # Create models dir
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ok("models/ directory ready")

    # Write files
    write_compose(plat, cpu_override)
    ok("docker-compose.yml generated")

    reg = load_registry()
    reg["platform"] = plat
    save_registry(reg)
    write_config(reg, cpu_override)

    # Start stack
    info("Starting stack...")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=str(BASE_DIR), capture_output=False
    )
    if result.returncode != 0:
        die("docker compose up failed. Is Docker running?")

    _install_aliases()

    print()
    ok(f"Stack running!")
    print(f"\n  {BOLD}Endpoints:{RESET}")
    print(f"    WebUI  →  {CYAN}http://localhost:{WEBUI_PORT}{RESET}")
    print(f"    API    →  {CYAN}http://localhost:{API_PORT}/v1{RESET}  (OpenAI-compatible)")
    print()
    info("Add your first model: lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M")
    print()


def cmd_pull(args):
    head("Lmux Pull")

    cpu_override = force_cpu_mode() or getattr(args, "cpu", False)
    if cpu_override:
        warn("CPU mode: model will run on RAM+CPU regardless of GPU availability.")

    token = get_hf_token()
    reg   = load_registry()

    # Resolve source → (repo_id, filename)
    source = args.source.strip()
    if source.startswith("http"):
        try:
            repo_id, filename = parse_hf_url(source)
        except ValueError as e:
            die(str(e))
    else:
        repo_id  = source
        filename = pick_gguf(repo_id, getattr(args, "quant", None))

    # Display name
    base_name    = Path(filename).name
    display_name = (
        args.name if args.name
        else re.sub(r"\.gguf$", "", base_name, flags=re.IGNORECASE).replace("_", "-").lower()
    )

    if display_name in reg["models"]:
        warn(f"'{display_name}' already registered.")
        info("Use --name <alias> to register it under a different name.")
        info(f"Or remove it first: lmux rm {display_name}")
        sys.exit(0)

    # Download
    local_path = download_gguf(repo_id, filename, token)
    file_size  = local_path.stat().st_size
    gguf_name  = local_path.name

    # VRAM estimate
    vram_gb, quant = estimate_vram(gguf_name, file_size)
    print()
    if vram_gb > VRAM_BUDGET_GB and not cpu_override:
        warn(f"Model needs ~{vram_gb:.1f}GB VRAM (budget: {VRAM_BUDGET_GB}GB)")
        warn("UNIFIED_MEMORY will overflow into your 32GB RAM. Inference will work but slower.")
        info("Consider a smaller quant (Q4_K_M or Q3_K_M) for full GPU speed.")
    else:
        ok(f"VRAM estimate: ~{vram_gb:.1f}GB / {VRAM_BUDGET_GB}GB — fits on GPU")

    # Register
    port = alloc_port(reg)
    reg["models"][display_name] = {
        "repo_id":          repo_id,
        "gguf_filename":    gguf_name,
        "port":             port,
        "quant":            quant,
        "vram_estimate_gb": vram_gb,
        "file_size_mb":     round(file_size / 1e6, 1),
        "cpu_only":         cpu_override,
        "added_at":         time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_registry(reg)
    write_config(reg, cpu_override and all(m.get("cpu_only") for m in reg["models"].values()))

    # Reload engine
    if stack_running():
        reload_engine()
        print()
        ok(f"'{display_name}' is ready in Open WebUI")
        info(f"WebUI → http://localhost:{WEBUI_PORT}")
        info(f"API   → http://localhost:{API_PORT}/v1  (model: {display_name})")
    else:
        ok(f"'{display_name}' registered.")
        warn("Stack not running. Start it: lmux start")
    print()


def cmd_list(args):
    reg    = load_registry()
    models = reg.get("models", {})

    if not models:
        info("No models registered. Pull one: lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M")
        return

    # Check which models are currently loaded
    loaded = _get_loaded_models()

    col = f"{{:<38}} {{:>6}} {{:>7}} {{:>9}} {{:<8}} {{}}"
    print()
    print(col.format("NAME", "PORT", "VRAM", "SIZE", "QUANT", "STATUS"))
    print("─" * 80)
    for name, m in models.items():
        vram  = f"{m.get('vram_estimate_gb','?')}GB"
        size  = f"{m.get('file_size_mb','?')}MB"
        quant = m.get("quant", "?")
        port  = m.get("port", "?")
        cpu   = " [CPU]" if m.get("cpu_only") else ""
        status = f"{GREEN}● loaded{RESET}" if name in loaded else f"{DIM}○ idle{RESET}"
        print(col.format(name + cpu, port, vram, size, quant, status))

    print()
    print(f"  {DIM}max_running: {MAX_RUNNING}  |  idle eviction: 300s  |  VRAM budget: {VRAM_BUDGET_GB}GB{RESET}")
    print(f"  {DIM}API → http://localhost:{API_PORT}/v1{RESET}")
    print()


def cmd_rm(args):
    reg  = load_registry()
    name = args.name

    if name not in reg["models"]:
        die(f"Model '{name}' not found. Run: lmux ls")

    meta = reg["models"].pop(name)
    save_registry(reg)
    write_config(reg)

    if args.delete_file:
        gguf_path = MODELS_DIR / meta["gguf_filename"]
        if gguf_path.exists():
            gguf_path.unlink()
            ok(f"Deleted: {gguf_path}")
        else:
            warn(f"File not found on disk: {gguf_path}")
    else:
        info(f"GGUF kept on disk. Use --delete-file to remove it.")

    if stack_running():
        reload_engine()

    ok(f"'{name}' removed.\n")


def cmd_down(args):
    """Remove from config, keep file on disk (soft disable)."""
    reg  = load_registry()
    name = args.name

    if name not in reg["models"]:
        die(f"Model '{name}' not found. Run: lmux ls")

    meta = reg["models"].pop(name)

    # Stash in inactive section for `up` to restore
    if "inactive" not in reg:
        reg["inactive"] = {}
    reg["inactive"][name] = meta

    save_registry(reg)
    write_config(reg)

    if stack_running():
        reload_engine()

    ok(f"'{name}' deactivated (GGUF kept on disk).")
    info(f"Bring it back with: lmux up {name}\n")


def cmd_up(args):
    """Add to config from model file on disk (re-enable)."""
    reg  = load_registry()
    name = args.name

    if name in reg["models"]:
        warn(f"'{name}' is already active.")
        return

    # Check inactive stash first
    inactive = reg.get("inactive", {})
    if name in inactive:
        meta = inactive.pop(name)
        # Verify file still exists
        gguf_path = MODELS_DIR / meta["gguf_filename"]
        if not gguf_path.exists():
            die(f"GGUF file not found: {gguf_path}")
        reg["models"][name] = meta
        save_registry(reg)
        write_config(reg)
        if stack_running():
            reload_engine()
        ok(f"'{name}' re-activated.\n")
        return

    # Fallback: scan models/ directory for a matching GGUF
    candidates = list(MODELS_DIR.glob("*.gguf"))
    match = None
    for c in candidates:
        if name.lower().replace("-", "").replace("_", "") in c.stem.lower().replace("-", "").replace("_", ""):
            match = c
            break

    if not match:
        die(f"No GGUF found for '{name}'. Available files:")
        for c in candidates:
            print(f"    {c.name}")
        sys.exit(1)

    file_size = match.stat().st_size
    vram_gb, quant = estimate_vram(match.name, file_size)
    port = alloc_port(reg)

    reg["models"][name] = {
        "repo_id":          "",
        "gguf_filename":    match.name,
        "port":             port,
        "quant":            quant,
        "vram_estimate_gb": vram_gb,
        "file_size_mb":     round(file_size / 1e6, 1),
        "cpu_only":         force_cpu_mode(),
        "added_at":         time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_registry(reg)
    write_config(reg)

    if stack_running():
        reload_engine()

    ok(f"'{name}' registered from {match.name}")
    ok(f"VRAM estimate: ~{vram_gb:.1f}GB\n")


def cmd_info(args):
    reg  = load_registry()
    name = args.name

    if name not in reg["models"]:
        die(f"Model '{name}' not found. Run: lmux ls")

    m = reg["models"][name]
    loaded = _get_loaded_models()
    status = f"{GREEN}● loaded{RESET}" if name in loaded else f"{DIM}○ idle{RESET}"

    head(f"Model: {name}")
    print(f"  {'Status':<22} {status}")
    for k, v in m.items():
        print(f"  {k:<22} {v}")

    vram     = m.get("vram_estimate_gb", 0)
    headroom = VRAM_BUDGET_GB - vram
    print()
    if headroom >= 0:
        print(f"  {'VRAM headroom':<22} ~{headroom:.1f}GB remaining after load")
    else:
        print(f"  {'VRAM overflow':<22} ~{abs(headroom):.1f}GB will use unified RAM (slower)")
    print()


def cmd_status(args):
    head("Lmux Status")

    if not stack_running():
        warn("Stack not running. Start with: lmux start")
        return

    loaded = _get_loaded_models()
    reg    = load_registry()

    # Engine health
    ok(f"Engine running  →  http://localhost:{API_PORT}/v1")
    ok(f"WebUI running   →  http://localhost:{WEBUI_PORT}")
    print()

    if loaded:
        info(f"Loaded models ({len(loaded)}/{MAX_RUNNING}):")
        for m in loaded:
            meta = reg["models"].get(m, {})
            vram = meta.get("vram_estimate_gb", "?")
            print(f"    {GREEN}●{RESET} {m}  (~{vram}GB VRAM)")
    else:
        info("No models currently loaded (idle — VRAM free)")

    total = len(reg["models"])
    idle  = total - len(loaded)
    print()
    info(f"{total} models registered  |  {len(loaded)} loaded  |  {idle} idle")

    plat = reg.get("platform", {})
    if plat.get("vram_gb"):
        print(f"  {DIM}GPU: {plat.get('gpu_name','')}  {plat.get('vram_gb','')}GB VRAM{RESET}")

    # Live NVIDIA VRAM stats (if available)
    _print_live_vram()
    print()


def cmd_stats(args):
    """
    Query llama-server's /metrics endpoint (Prometheus format)
    for token throughput and usage stats.
    """
    import urllib.request

    head("Token Stats")

    if not stack_running():
        warn("Stack not running.")
        return

    loaded = _get_loaded_models()
    reg    = load_registry()

    if not loaded:
        info("No models currently loaded. Stats are only available for active models.")
        return

    for model_name in loaded:
        meta = reg["models"].get(model_name, {})
        port = meta.get("port")
        if not port:
            continue

        print(f"\n  {BOLD}{model_name}{RESET}")
        try:
            url = f"http://localhost:{port}/metrics"
            with urllib.request.urlopen(url, timeout=3) as resp:
                raw = resp.read().decode()

            metrics = _parse_prometheus(raw)

            fields = {
                "llamacpp:prompt_tokens_total":      ("Prompt tokens total",    ""),
                "llamacpp:tokens_predicted_total":   ("Generated tokens total", ""),
                "llamacpp:tokens_predicted_seconds": ("Generation time",        "s"),
                "llamacpp:prompt_tokens_seconds":    ("Prompt eval time",       "s"),
                "llamacpp:kv_cache_usage_ratio":     ("KV cache usage",         "%"),
                "llamacpp:requests_processing":      ("Active requests",        ""),
                "llamacpp:requests_deferred":        ("Queued requests",        ""),
            }
            for key, (label, unit) in fields.items():
                val = metrics.get(key)
                if val is not None:
                    if unit == "%":
                        val = f"{float(val)*100:.1f}%"
                    elif unit == "s":
                        val = f"{float(val):.2f}s"
                    print(f"    {label:<28} {val}")

            # Derived: tokens/sec
            gen_toks = float(metrics.get("llamacpp:tokens_predicted_total", 0))
            gen_secs = float(metrics.get("llamacpp:tokens_predicted_seconds", 0))
            if gen_secs > 0:
                tps = gen_toks / gen_secs
                print(f"    {'Generation speed':<28} {tps:.1f} tok/s")

        except Exception as e:
            warn(f"Could not reach metrics for {model_name} on port {port}: {e}")
            info("Metrics available after first inference request.")
    print()


def cmd_run(args):
    """One-shot CLI inference — ollama-style."""
    import urllib.request
    import json as _json

    model  = args.model
    prompt = args.prompt
    reg    = load_registry()

    if model not in reg["models"] and model != "auto":
        die(f"Model '{model}' not registered. Run: lmux ls")

    if not stack_running():
        die("Stack not running. Start with: lmux start")

    head(f"lmux run  {model}")
    print(f"  {DIM}{prompt}{RESET}\n")

    payload = _json.dumps({
        "model":      model if model != "auto" else list(reg["models"].keys())[0],
        "messages":   [{"role": "user", "content": prompt}],
        "stream":     True,
        "max_tokens": getattr(args, "max_tokens", 512),
    }).encode()

    req = urllib.request.Request(
        f"http://localhost:{API_PORT}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    t0 = time.time()
    token_count = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.decode().strip()
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    chunk = _json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        print(delta, end="", flush=True)
                        token_count += 1
    except KeyboardInterrupt:
        pass

    elapsed = time.time() - t0
    print(f"\n\n  {DIM}─── {token_count} tokens  {elapsed:.1f}s  {token_count/elapsed:.1f} tok/s ───{RESET}\n")


def cmd_reload(args):
    reg = load_registry()
    write_config(reg)
    if stack_running():
        reload_engine()
        ok("Engine reloaded.\n")
    else:
        ok("Config updated. Start stack with: lmux start\n")


def cmd_start(args):
    info("Starting stack...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=str(BASE_DIR))
    print()
    ok(f"WebUI → http://localhost:{WEBUI_PORT}")
    ok(f"API   → http://localhost:{API_PORT}/v1")
    print()


def cmd_stop(args):
    info("Stopping stack...")
    subprocess.run(["docker", "compose", "down"], cwd=str(BASE_DIR))
    ok("Stack stopped. Models unloaded. VRAM freed.\n")


# ── INTERNAL HELPERS ──────────────────────────────────────────────────────────

def _get_loaded_models() -> list[str]:
    """Ask llama-swap which models are currently running."""
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(
            f"http://localhost:{API_PORT}/v1/models", timeout=2
        ) as resp:
            data = _json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


def _print_live_vram():
    """Print real-time NVIDIA GPU VRAM usage (if nvidia-smi is available)."""
    nvidia = shutil.which("nvidia-smi")
    if not nvidia:
        return
    try:
        out = subprocess.run(
            [nvidia, "--query-gpu=memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return
        line = out.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        used_mb, total_mb, free_mb = int(parts[0]), int(parts[1]), int(parts[2])
        gpu_util = parts[3]
        temp_c   = parts[4]
        pct = (used_mb / total_mb * 100) if total_mb > 0 else 0

        # Build a VRAM bar
        bar_len = 30
        filled  = int(bar_len * pct / 100)
        bar_color = GREEN if pct < 70 else (YELLOW if pct < 90 else RED)
        bar = f"{bar_color}{'█' * filled}{DIM}{'░' * (bar_len - filled)}{RESET}"

        print()
        info("Live GPU Stats:")
        print(f"    VRAM:  [{bar}] {used_mb}MB / {total_mb}MB ({pct:.0f}%)")
        print(f"    Free:  {free_mb}MB  |  GPU Load: {gpu_util}%  |  Temp: {temp_c}°C")
    except Exception:
        pass


def _parse_prometheus(raw: str) -> dict:
    """Parse Prometheus /metrics text format into a flat dict."""
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            result[parts[0].split("{")[0]] = parts[1]
    return result


def _install_aliases():
    """Add lmux shell alias to the appropriate profile file."""
    lmux_path = Path(__file__).resolve()
    system     = platform.system()

    if system in ("Linux", "Darwin"):
        shell    = os.environ.get("SHELL", "bash")
        profile  = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
        alias_line = f"\nalias lmux='python3 {lmux_path}'\n"

        existing = profile.read_text() if profile.exists() else ""
        if "alias lmux=" not in existing:
            with open(profile, "a") as f:
                f.write(f"\n# lmux — Language Model Multiplexer\n")
                f.write(alias_line)
            ok(f"Alias added to {profile}")
            info(f"Run: source {profile}  (or open a new terminal)")
        else:
            ok(f"Alias already in {profile}")

    elif system == "Windows":
        ps_line = f'\nfunction lmux {{ python "{lmux_path}" @args }}\n'
        try:
            profile_path = subprocess.run(
                ["powershell", "-Command", "echo $PROFILE"],
                capture_output=True, text=True
            ).stdout.strip()
            profile = Path(profile_path)
            profile.parent.mkdir(parents=True, exist_ok=True)
            existing = profile.read_text() if profile.exists() else ""
            if "function lmux" not in existing:
                with open(profile, "a") as f:
                    f.write(f"\n# lmux — Language Model Multiplexer\n")
                    f.write(ps_line)
                ok(f"Alias added to {profile}")
                info("Restart PowerShell or run: . $PROFILE")
            else:
                ok(f"Alias already in {profile}")
        except Exception as e:
            warn(f"Could not write PowerShell profile: {e}")
            info(f"Add manually: function lmux {{ python \"{lmux_path}\" @args }}")


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="lmux",
        description="Language Model Multiplexer — Ollama-style CLI for llama-swap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""
        Examples:
          lmux init
          lmux pull bartowski/Meta-Llama-3-8B-Instruct-GGUF --quant Q4_K_M
          lmux pull https://huggingface.co/TheBloke/Mistral-7B-v0.1-GGUF/blob/main/mistral-7b-v0.1.Q4_K_M.gguf
          lmux pull bartowski/Qwen2.5-7B-Instruct-GGUF --quant Q4_K_M --name qwen2.5-7b
          lmux ls
          lmux run llama3-8b "Explain attention mechanisms in 3 sentences"
          lmux stats
          lmux rm llama3-8b --delete-file
          lmux stop

        Force CPU mode (any command):
          CUDA=0 lmux pull ...
          lmux pull ... --cpu

        Programmatic access (OpenAI SDK):
          from openai import OpenAI
          client = OpenAI(base_url="http://localhost:{API_PORT}/v1", api_key="lmux-local")
        """)
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Detect GPU/OS, scaffold and start the stack")
    p_init.add_argument("--cpu", action="store_true", help="Force CPU mode even if GPU present")

    # pull
    p_pull = sub.add_parser("pull", help="Download a model from HuggingFace")
    p_pull.add_argument("source", help="HF URL  or  owner/repo  shorthand")
    p_pull.add_argument("--quant", default="Q4_K_M", help="Quant filter (default: Q4_K_M)")
    p_pull.add_argument("--name", help="Custom display name shown in Open WebUI")
    p_pull.add_argument("--cpu", action="store_true", help="Force this model to run on CPU/RAM")

    # ls / list
    sub.add_parser("ls",   help="List registered models")
    sub.add_parser("list", help="List registered models (alias for ls)")

    # rm
    p_rm = sub.add_parser("rm", help="Remove a model")
    p_rm.add_argument("name", help="Model name as shown in lmux ls")
    p_rm.add_argument("--delete-file", action="store_true", help="Also delete the GGUF from disk")

    # down (soft disable)
    p_down = sub.add_parser("down", help="Deactivate model (keep file on disk)")
    p_down.add_argument("name", help="Model name to deactivate")

    # up (re-enable)
    p_up = sub.add_parser("up", help="Activate model from file on disk")
    p_up.add_argument("name", help="Model name to activate")

    # info
    p_info = sub.add_parser("info", help="Show model metadata and VRAM details")
    p_info.add_argument("name")

    # status
    sub.add_parser("status", help="Show running stack and loaded models")

    # stats
    sub.add_parser("stats", help="Token throughput and usage from /metrics")

    # run
    p_run = sub.add_parser("run", help="One-shot CLI inference")
    p_run.add_argument("model", help="Model name (or 'auto' for first registered)")
    p_run.add_argument("prompt", help="Prompt string (quote it)")
    p_run.add_argument("--max-tokens", type=int, default=512)

    # reload
    sub.add_parser("reload", help="Resync config.yaml and restart engine")

    # start / stop
    sub.add_parser("start", help="Start the Docker stack")
    sub.add_parser("stop",  help="Stop the stack and free all VRAM")

    args = parser.parse_args()

    dispatch = {
        "init":   cmd_init,
        "pull":   cmd_pull,
        "ls":     cmd_list,
        "list":   cmd_list,
        "rm":     cmd_rm,
        "down":   cmd_down,
        "up":     cmd_up,
        "info":   cmd_info,
        "status": cmd_status,
        "stats":  cmd_stats,
        "run":    cmd_run,
        "reload": cmd_reload,
        "start":  cmd_start,
        "stop":   cmd_stop,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
