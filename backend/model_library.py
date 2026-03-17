"""
EthOS — Model Library
Katalog modeli LLM z automatycznym dopasowaniem do sprzętu,
pobieraniem z Hugging Face i zarządzaniem ścieżką.
Includes hardware-agnostic benchmark, tier system, and CPU feature detection.
"""

import json
import os
import platform
import shutil
import time
import threading

import psutil

try:
    import GPUtil
    _HAS_GPU = True
except ImportError:
    _HAS_GPU = False

os.environ.setdefault('HF_HUB_DISABLE_XET', '1')   # xet conflicts with gevent
try:
    from huggingface_hub import hf_hub_download, HfApi
    _HAS_HF = True
except ImportError:
    _HAS_HF = False

from host import data_path, host_run as _host_run


# ═══════════════════════════════════════════════════════════════════
#  Hardware: CPU features, NPU, full discovery
# ═══════════════════════════════════════════════════════════════════

def get_cpu_features():
    """Detect CPU instruction set features: AVX2, AVX-512, VNNI, SSE4.2, NEON."""
    features = {
        'avx2': False,
        'avx512': False,
        'vnni': False,
        'sse42': False,
        'neon': False,
        'cores_physical': psutil.cpu_count(logical=False) or 1,
        'cores_logical': psutil.cpu_count(logical=True) or 1,
        'cpu_name': '',
        'arch': platform.machine(),
        'optimal_threads': max(1, (psutil.cpu_count(logical=False) or 2) - 1),
    }
    # ARM architecture
    if features['arch'] in ('aarch64', 'arm64'):
        features['neon'] = True

    try:
        r = _host_run("cat /proc/cpuinfo 2>/dev/null | head -80", timeout=5)
        if r.returncode == 0 and r.stdout:
            for line in r.stdout.splitlines():
                if line.startswith('model name'):
                    features['cpu_name'] = line.split(':', 1)[-1].strip()
                if line.startswith('flags'):
                    flags = line.split(':', 1)[-1].lower()
                    features['avx2'] = 'avx2' in flags
                    features['avx512'] = 'avx512f' in flags
                    features['vnni'] = ('avx512_vnni' in flags or 'avx_vnni' in flags)
                    features['sse42'] = 'sse4_2' in flags
                    break
    except Exception:
        pass

    # Optimal quantization recommendation
    if features['vnni'] or features['avx512']:
        features['recommended_quant'] = 'Q4_K_M'
        features['quant_reason'] = 'AVX-512/VNNI — Q4_K_M offers best speed/quality ratio'
    elif features['avx2']:
        features['recommended_quant'] = 'Q4_K_M'
        features['quant_reason'] = 'AVX2 — Q4_K_M is optimal for this CPU'
    elif features['neon']:
        features['recommended_quant'] = 'Q4_K_M'
        features['quant_reason'] = 'ARM NEON — Q4_K_M works well'
    else:
        features['recommended_quant'] = 'Q4_0'
        features['quant_reason'] = 'Basic CPU — Q4_0 for maximum compatibility'

    return features


def get_npu_info():
    """Detect NPU / AI accelerators (Intel NPU, Coral TPU, etc.)."""
    npus = []
    try:
        # Intel NPU (Meteor Lake+)
        r = _host_run("ls /dev/accel* 2>/dev/null", timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            npus.append({
                'type': 'intel_npu',
                'name': 'Intel NPU',
                'device': r.stdout.strip().split('\n')[0],
            })
    except Exception:
        pass
    try:
        # Google Coral TPU
        r = _host_run(
            "ls /dev/apex_0 2>/dev/null || "
            "lsusb 2>/dev/null | grep -i 'global unichip\\|google.*coral'",
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            npus.append({'type': 'coral_tpu', 'name': 'Google Coral TPU'})
    except Exception:
        pass
    return npus


def get_full_hardware_info():
    """Comprehensive hardware discovery: RAM, GPU, CPU features, NPU, disk."""
    hw = get_hardware_info()
    cpu = get_cpu_features()
    npu = get_npu_info()
    hw['cpu'] = cpu
    hw['npus'] = npu
    hw['has_npu'] = len(npu) > 0
    return hw


# ═══════════════════════════════════════════════════════════════════
#  Performance tiers
# ═══════════════════════════════════════════════════════════════════

def get_tier(tps):
    """Map tokens-per-second to a performance tier profile."""
    if tps >= 15:
        return {
            'id': 'ultra',
            'name': 'Ultra',
            'icon': 'fa-bolt',
            'color': '#10b981',
            'description': 'Szybki, płynny czat — idealne doświadczenie',
            'recommended_params': '8B+',
            'max_context': 8192,
        }
    elif tps >= 5:
        return {
            'id': 'balanced',
            'name': 'Balanced',
            'icon': 'fa-balance-scale',
            'color': '#f59e0b',
            'description': 'Dobry balans szybkości i jakości',
            'recommended_params': '3B–7B',
            'max_context': 4096,
        }
    else:
        return {
            'id': 'light',
            'name': 'Light',
            'icon': 'fa-feather',
            'color': '#ef4444',
            'description': 'Podstawowe działanie, mały model',
            'recommended_params': '1B–3B',
            'max_context': 2048,
        }


def get_tier_for_hardware(hw=None):
    """Estimate tier based on hardware specs (without running a benchmark)."""
    if hw is None:
        hw = get_hardware_info()
    ram = hw.get('ram_total_gb', 0)
    cpu_info = hw.get('cpu', get_cpu_features())
    cores = cpu_info.get('cores_physical', 1)

    # Rough heuristic
    if ram >= 16 and cores >= 4:
        return get_tier(20)   # Ultra
    elif ram >= 8 and cores >= 2:
        return get_tier(8)    # Balanced
    else:
        return get_tier(3)    # Light

MODEL_CATALOG = [
    {
        'id': 'llama3-8b-q4',
        'name': 'Llama 3 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q4_K_M',
        'size_gb': 4.9,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'Meta Llama 3 — szybki, wszechstronny. Najlepszy stosunek jakość/wydajność.',
        'hf_repo': 'bartowski/Meta-Llama-3-8B-Instruct-GGUF',
        'hf_filename': 'Meta-Llama-3-8B-Instruct-Q4_K_M.gguf',
        'context_length': 8192,
        'license': 'Llama 3 Community',
        'languages': ['en', 'pl'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    {
        'id': 'llama3-8b-q8',
        'name': 'Llama 3 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q8_0',
        'size_gb': 8.5,
        'ram_required_gb': 11,
        'vram_required_gb': 9,
        'description': 'Meta Llama 3 — wyższa jakość z Q8, wymaga więcej RAM.',
        'hf_repo': 'bartowski/Meta-Llama-3-8B-Instruct-GGUF',
        'hf_filename': 'Meta-Llama-3-8B-Instruct-Q8_0.gguf',
        'context_length': 8192,
        'license': 'Llama 3 Community',
        'languages': ['en', 'pl'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    {
        'id': 'llama31-8b-q4',
        'name': 'Llama 3.1 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q4_K_M',
        'size_gb': 4.9,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'Llama 3.1 — dłuższy kontekst (128K), ulepszone instrukcje.',
        'hf_repo': 'bartowski/Meta-Llama-3.1-8B-Instruct-GGUF',
        'hf_filename': 'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Llama 3.1 Community',
        'languages': ['en', 'pl'],
        'use_cases': ['chat', 'code', 'reasoning', 'long-context'],
    },
    {
        'id': 'llama31-8b-q8',
        'name': 'Llama 3.1 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q8_0',
        'size_gb': 8.5,
        'ram_required_gb': 11,
        'vram_required_gb': 9,
        'description': 'Llama 3.1 Q8 — wyższa jakość, dłuższy kontekst.',
        'hf_repo': 'bartowski/Meta-Llama-3.1-8B-Instruct-GGUF',
        'hf_filename': 'Meta-Llama-3.1-8B-Instruct-Q8_0.gguf',
        'context_length': 131072,
        'license': 'Llama 3.1 Community',
        'languages': ['en', 'pl'],
        'use_cases': ['chat', 'code', 'reasoning', 'long-context'],
    },
    {
        'id': 'mistral-7b-q4',
        'name': 'Mistral 7B Instruct v0.3',
        'family': 'Mistral',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.4,
        'ram_required_gb': 6.5,
        'vram_required_gb': 5,
        'description': 'Mistral 7B — lekki, szybki, dobry do chatu i kodu.',
        'hf_repo': 'bartowski/Mistral-7B-Instruct-v0.3-GGUF',
        'hf_filename': 'Mistral-7B-Instruct-v0.3-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'pl', 'fr', 'de', 'es'],
        'use_cases': ['chat', 'code'],
    },
    {
        'id': 'mistral-7b-q8',
        'name': 'Mistral 7B Instruct v0.3',
        'family': 'Mistral',
        'params': '7B',
        'quant': 'Q8_0',
        'size_gb': 7.7,
        'ram_required_gb': 10,
        'vram_required_gb': 8,
        'description': 'Mistral 7B Q8 — lepsza jakość odpowiedzi.',
        'hf_repo': 'bartowski/Mistral-7B-Instruct-v0.3-GGUF',
        'hf_filename': 'Mistral-7B-Instruct-v0.3-Q8_0.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'pl', 'fr', 'de', 'es'],
        'use_cases': ['chat', 'code'],
    },
    {
        'id': 'phi3-mini-q4',
        'name': 'Phi-3 Mini 3.8B',
        'family': 'Phi',
        'params': '3.8B',
        'quant': 'Q4_K_M',
        'size_gb': 2.4,
        'ram_required_gb': 4,
        'vram_required_gb': 3,
        'description': 'Microsoft Phi-3 Mini — ultra-lekki, zaskakująco zdolny. Idealny do słabszych maszyn.',
        'hf_repo': 'bartowski/Phi-3-mini-4k-instruct-GGUF',
        'hf_filename': 'Phi-3-mini-4k-instruct-Q4_K_M.gguf',
        'context_length': 4096,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    {
        'id': 'phi3-mini-q8',
        'name': 'Phi-3 Mini 3.8B',
        'family': 'Phi',
        'params': '3.8B',
        'quant': 'Q8_0',
        'size_gb': 4.1,
        'ram_required_gb': 6,
        'vram_required_gb': 5,
        'description': 'Phi-3 Mini Q8 — lepsza jakość dla szybkiej inferencji.',
        'hf_repo': 'bartowski/Phi-3-mini-4k-instruct-GGUF',
        'hf_filename': 'Phi-3-mini-4k-instruct-Q8_0.gguf',
        'context_length': 4096,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    {
        'id': 'phi3-medium-14b-q4',
        'name': 'Phi-3 Medium 14B',
        'family': 'Phi',
        'params': '14B',
        'quant': 'Q4_K_M',
        'size_gb': 8.6,
        'ram_required_gb': 11,
        'vram_required_gb': 9,
        'description': 'Phi-3 Medium — duży model Microsoft, mocny w rozumowaniu i kodzie.',
        'hf_repo': 'bartowski/Phi-3-medium-4k-instruct-GGUF',
        'hf_filename': 'Phi-3-medium-4k-instruct-Q4_K_M.gguf',
        'context_length': 4096,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    {
        'id': 'gemma2-9b-q4',
        'name': 'Gemma 2 9B',
        'family': 'Gemma',
        'params': '9B',
        'quant': 'Q4_K_M',
        'size_gb': 5.8,
        'ram_required_gb': 8,
        'vram_required_gb': 6,
        'description': 'Google Gemma 2 — wielojęzyczny, dobry do konwersacji i reasoning.',
        'hf_repo': 'bartowski/gemma-2-9b-it-GGUF',
        'hf_filename': 'gemma-2-9b-it-Q4_K_M.gguf',
        'context_length': 8192,
        'license': 'Gemma',
        'languages': ['en', 'pl', 'de', 'fr', 'es', 'ja'],
        'use_cases': ['chat', 'reasoning', 'multilingual'],
    },
    {
        'id': 'gemma2-9b-q8',
        'name': 'Gemma 2 9B',
        'family': 'Gemma',
        'params': '9B',
        'quant': 'Q8_0',
        'size_gb': 10.1,
        'ram_required_gb': 13,
        'vram_required_gb': 11,
        'description': 'Gemma 2 Q8 — wyższa jakość generacji.',
        'hf_repo': 'bartowski/gemma-2-9b-it-GGUF',
        'hf_filename': 'gemma-2-9b-it-Q8_0.gguf',
        'context_length': 8192,
        'license': 'Gemma',
        'languages': ['en', 'pl', 'de', 'fr', 'es', 'ja'],
        'use_cases': ['chat', 'reasoning', 'multilingual'],
    },
    {
        'id': 'qwen25-7b-q4',
        'name': 'Qwen 2.5 7B',
        'family': 'Qwen',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.7,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'Alibaba Qwen 2.5 — bardzo dobry w kodowaniu i matematyce.',
        'hf_repo': 'bartowski/Qwen2.5-7B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-7B-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat', 'code', 'math', 'reasoning'],
    },
    {
        'id': 'qwen25-14b-q4',
        'name': 'Qwen 2.5 14B',
        'family': 'Qwen',
        'params': '14B',
        'quant': 'Q4_K_M',
        'size_gb': 9,
        'ram_required_gb': 12,
        'vram_required_gb': 10,
        'description': 'Qwen 2.5 14B — mocniejsza wersja, coding benchmark champion.',
        'hf_repo': 'bartowski/Qwen2.5-14B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-14B-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat', 'code', 'math', 'reasoning'],
    },
    {
        'id': 'codellama-7b-q4',
        'name': 'CodeLlama 7B Instruct',
        'family': 'CodeLlama',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.2,
        'ram_required_gb': 6.5,
        'vram_required_gb': 5,
        'description': 'Meta CodeLlama — specjalizowany do generowania i analizy kodu.',
        'hf_repo': 'TheBloke/CodeLlama-7B-Instruct-GGUF',
        'hf_filename': 'codellama-7b-instruct.Q4_K_M.gguf',
        'context_length': 16384,
        'license': 'Llama 2 Community',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    # ── Tiny / Ultra-light ──────────────────────────────────────────
    {
        'id': 'tinyllama-1b-q4',
        'name': 'TinyLlama 1.1B Chat',
        'family': 'Llama',
        'params': '1.1B',
        'quant': 'Q4_K_M',
        'size_gb': 0.7,
        'ram_required_gb': 2,
        'vram_required_gb': 1,
        'description': 'Ultralekki model 1.1B — idealny do testów i bardzo słabego sprzętu.',
        'hf_repo': 'TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF',
        'hf_filename': 'tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf',
        'context_length': 2048,
        'license': 'Apache 2.0',
        'languages': ['en'],
        'use_cases': ['chat'],
    },
    {
        'id': 'tinyllama-1b-q8',
        'name': 'TinyLlama 1.1B Chat',
        'family': 'Llama',
        'params': '1.1B',
        'quant': 'Q8_0',
        'size_gb': 1.2,
        'ram_required_gb': 3,
        'vram_required_gb': 2,
        'description': 'TinyLlama Q8 — miniaturowy, ale lepsza jakość niż Q4.',
        'hf_repo': 'TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF',
        'hf_filename': 'tinyllama-1.1b-chat-v1.0.Q8_0.gguf',
        'context_length': 2048,
        'license': 'Apache 2.0',
        'languages': ['en'],
        'use_cases': ['chat'],
    },
    # ── Llama 3.2 (small) ──────────────────────────────────────────
    {
        'id': 'llama32-1b-q4',
        'name': 'Llama 3.2 1B',
        'family': 'Llama',
        'params': '1B',
        'quant': 'Q4_K_M',
        'size_gb': 0.8,
        'ram_required_gb': 2,
        'vram_required_gb': 1,
        'description': 'Meta Llama 3.2 1B — najlżejszy Llama, błyskawiczna odpowiedź.',
        'hf_repo': 'bartowski/Llama-3.2-1B-Instruct-GGUF',
        'hf_filename': 'Llama-3.2-1B-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Llama 3.2 Community',
        'languages': ['en', 'pl', 'de', 'fr', 'es'],
        'use_cases': ['chat'],
    },
    {
        'id': 'llama32-1b-q8',
        'name': 'Llama 3.2 1B',
        'family': 'Llama',
        'params': '1B',
        'quant': 'Q8_0',
        'size_gb': 1.3,
        'ram_required_gb': 3,
        'vram_required_gb': 2,
        'description': 'Llama 3.2 1B Q8 — pełna precyzja w mini opakowaniu.',
        'hf_repo': 'bartowski/Llama-3.2-1B-Instruct-GGUF',
        'hf_filename': 'Llama-3.2-1B-Instruct-Q8_0.gguf',
        'context_length': 131072,
        'license': 'Llama 3.2 Community',
        'languages': ['en', 'pl', 'de', 'fr', 'es'],
        'use_cases': ['chat'],
    },
    {
        'id': 'llama32-3b-q4',
        'name': 'Llama 3.2 3B',
        'family': 'Llama',
        'params': '3B',
        'quant': 'Q4_K_M',
        'size_gb': 2.0,
        'ram_required_gb': 4,
        'vram_required_gb': 3,
        'description': 'Llama 3.2 3B — świetny balans lekkości i jakości. Wielojęzyczny.',
        'hf_repo': 'bartowski/Llama-3.2-3B-Instruct-GGUF',
        'hf_filename': 'Llama-3.2-3B-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Llama 3.2 Community',
        'languages': ['en', 'pl', 'de', 'fr', 'es'],
        'use_cases': ['chat', 'reasoning'],
    },
    {
        'id': 'llama32-3b-q8',
        'name': 'Llama 3.2 3B',
        'family': 'Llama',
        'params': '3B',
        'quant': 'Q8_0',
        'size_gb': 3.4,
        'ram_required_gb': 5.5,
        'vram_required_gb': 4,
        'description': 'Llama 3.2 3B Q8 — wyższa precyzja, nadal lekki.',
        'hf_repo': 'bartowski/Llama-3.2-3B-Instruct-GGUF',
        'hf_filename': 'Llama-3.2-3B-Instruct-Q8_0.gguf',
        'context_length': 131072,
        'license': 'Llama 3.2 Community',
        'languages': ['en', 'pl', 'de', 'fr', 'es'],
        'use_cases': ['chat', 'reasoning'],
    },
    # ── Gemma 2 2B (lightweight) ───────────────────────────────────
    {
        'id': 'gemma2-2b-q4',
        'name': 'Gemma 2 2B',
        'family': 'Gemma',
        'params': '2B',
        'quant': 'Q4_K_M',
        'size_gb': 1.5,
        'ram_required_gb': 3,
        'vram_required_gb': 2,
        'description': 'Google Gemma 2 2B — maleńki, szybki, wielojęzyczny.',
        'hf_repo': 'bartowski/gemma-2-2b-it-GGUF',
        'hf_filename': 'gemma-2-2b-it-Q4_K_M.gguf',
        'context_length': 8192,
        'license': 'Gemma',
        'languages': ['en', 'pl', 'de', 'fr', 'es', 'ja'],
        'use_cases': ['chat', 'multilingual'],
    },
    {
        'id': 'gemma2-2b-q8',
        'name': 'Gemma 2 2B',
        'family': 'Gemma',
        'params': '2B',
        'quant': 'Q8_0',
        'size_gb': 2.7,
        'ram_required_gb': 4.5,
        'vram_required_gb': 3,
        'description': 'Gemma 2 2B Q8 — pełna jakość w kompaktowym modelu.',
        'hf_repo': 'bartowski/gemma-2-2b-it-GGUF',
        'hf_filename': 'gemma-2-2b-it-Q8_0.gguf',
        'context_length': 8192,
        'license': 'Gemma',
        'languages': ['en', 'pl', 'de', 'fr', 'es', 'ja'],
        'use_cases': ['chat', 'multilingual'],
    },
    # ── Qwen 2.5 small variants ───────────────────────────────────
    {
        'id': 'qwen25-0.5b-q8',
        'name': 'Qwen 2.5 0.5B',
        'family': 'Qwen',
        'params': '0.5B',
        'quant': 'Q8_0',
        'size_gb': 0.5,
        'ram_required_gb': 2,
        'vram_required_gb': 1,
        'description': 'Qwen 2.5 0.5B — najlżejszy Qwen, natychmiastowe odpowiedzi.',
        'hf_repo': 'bartowski/Qwen2.5-0.5B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-0.5B-Instruct-Q8_0.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat'],
    },
    {
        'id': 'qwen25-1.5b-q4',
        'name': 'Qwen 2.5 1.5B',
        'family': 'Qwen',
        'params': '1.5B',
        'quant': 'Q4_K_M',
        'size_gb': 1.0,
        'ram_required_gb': 2.5,
        'vram_required_gb': 1.5,
        'description': 'Qwen 2.5 1.5B — lekki, wielojęzyczny, zaskakująco zdolny.',
        'hf_repo': 'bartowski/Qwen2.5-1.5B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-1.5B-Instruct-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat', 'code'],
    },
    {
        'id': 'qwen25-3b-q4',
        'name': 'Qwen 2.5 3B',
        'family': 'Qwen',
        'params': '3B',
        'quant': 'Q4_K_M',
        'size_gb': 2.0,
        'ram_required_gb': 4,
        'vram_required_gb': 3,
        'description': 'Qwen 2.5 3B — kompaktowy, mocny w kodowaniu i matematyce.',
        'hf_repo': 'bartowski/Qwen2.5-3B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-3B-Instruct-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat', 'code', 'math'],
    },
    {
        'id': 'qwen25-3b-q8',
        'name': 'Qwen 2.5 3B',
        'family': 'Qwen',
        'params': '3B',
        'quant': 'Q8_0',
        'size_gb': 3.4,
        'ram_required_gb': 5.5,
        'vram_required_gb': 4,
        'description': 'Qwen 2.5 3B Q8 — wyższa precyzja, lepsze odpowiedzi.',
        'hf_repo': 'bartowski/Qwen2.5-3B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-3B-Instruct-Q8_0.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh', 'pl'],
        'use_cases': ['chat', 'code', 'math'],
    },
    # ── Qwen 2.5 Coder (specialized) ──────────────────────────────
    {
        'id': 'qwen25-coder-1.5b-q4',
        'name': 'Qwen 2.5 Coder 1.5B',
        'family': 'Qwen',
        'params': '1.5B',
        'quant': 'Q4_K_M',
        'size_gb': 1.0,
        'ram_required_gb': 2.5,
        'vram_required_gb': 1.5,
        'description': 'Qwen Coder 1.5B — miniaturowy asystent kodowania.',
        'hf_repo': 'bartowski/Qwen2.5-Coder-1.5B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    {
        'id': 'qwen25-coder-7b-q4',
        'name': 'Qwen 2.5 Coder 7B',
        'family': 'Qwen',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.7,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'Qwen Coder 7B — specjalizowany asystent kodowania, benchmark champion.',
        'hf_repo': 'bartowski/Qwen2.5-Coder-7B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    {
        'id': 'qwen25-coder-7b-q8',
        'name': 'Qwen 2.5 Coder 7B',
        'family': 'Qwen',
        'params': '7B',
        'quant': 'Q8_0',
        'size_gb': 8.1,
        'ram_required_gb': 11,
        'vram_required_gb': 9,
        'description': 'Qwen Coder 7B Q8 — wyższa jakość generowania kodu.',
        'hf_repo': 'bartowski/Qwen2.5-Coder-7B-Instruct-GGUF',
        'hf_filename': 'Qwen2.5-Coder-7B-Instruct-Q8_0.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    # ── Phi-3.5 Mini ───────────────────────────────────────────────
    {
        'id': 'phi35-mini-q4',
        'name': 'Phi-3.5 Mini 3.8B',
        'family': 'Phi',
        'params': '3.8B',
        'quant': 'Q4_K_M',
        'size_gb': 2.4,
        'ram_required_gb': 4,
        'vram_required_gb': 3,
        'description': 'Microsoft Phi-3.5 Mini — ulepszona wersja, lepsza od Phi-3 w rozumowaniu.',
        'hf_repo': 'bartowski/Phi-3.5-mini-instruct-GGUF',
        'hf_filename': 'Phi-3.5-mini-instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning', 'long-context'],
    },
    {
        'id': 'phi35-mini-q8',
        'name': 'Phi-3.5 Mini 3.8B',
        'family': 'Phi',
        'params': '3.8B',
        'quant': 'Q8_0',
        'size_gb': 4.1,
        'ram_required_gb': 6,
        'vram_required_gb': 5,
        'description': 'Phi-3.5 Mini Q8 — pełna jakość z długim kontekstem 128K.',
        'hf_repo': 'bartowski/Phi-3.5-mini-instruct-GGUF',
        'hf_filename': 'Phi-3.5-mini-instruct-Q8_0.gguf',
        'context_length': 131072,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning', 'long-context'],
    },
    # ── Phi-4 ──────────────────────────────────────────────────────
    {
        'id': 'phi4-14b-q4',
        'name': 'Phi-4 14B',
        'family': 'Phi',
        'params': '14B',
        'quant': 'Q4_K_M',
        'size_gb': 8.4,
        'ram_required_gb': 11,
        'vram_required_gb': 9,
        'description': 'Microsoft Phi-4 — najnowszy Phi, topowa jakość reasoning i kodu.',
        'hf_repo': 'bartowski/phi-4-GGUF',
        'hf_filename': 'phi-4-Q4_K_M.gguf',
        'context_length': 16384,
        'license': 'MIT',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning', 'math'],
    },
    # ── Mistral Nemo ───────────────────────────────────────────────
    {
        'id': 'mistral-nemo-12b-q4',
        'name': 'Mistral Nemo 12B',
        'family': 'Mistral',
        'params': '12B',
        'quant': 'Q4_K_M',
        'size_gb': 7.5,
        'ram_required_gb': 10,
        'vram_required_gb': 8,
        'description': 'Mistral Nemo 12B — mocniejszy niż 7B, wielojęzyczny, duży kontekst.',
        'hf_repo': 'bartowski/Mistral-Nemo-Instruct-2407-GGUF',
        'hf_filename': 'Mistral-Nemo-Instruct-2407-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'Apache 2.0',
        'languages': ['en', 'pl', 'fr', 'de', 'es', 'it', 'pt'],
        'use_cases': ['chat', 'code', 'multilingual', 'long-context'],
    },
    # ── Mistral Small ──────────────────────────────────────────────
    {
        'id': 'mistral-small-24b-q4',
        'name': 'Mistral Small 22B',
        'family': 'Mistral',
        'params': '22B',
        'quant': 'Q4_K_M',
        'size_gb': 13.5,
        'ram_required_gb': 18,
        'vram_required_gb': 14,
        'description': 'Mistral Small 22B — potężny, wymagający sprzętu. Zbliżona jakość do GPT-3.5.',
        'hf_repo': 'bartowski/Mistral-Small-Instruct-2409-GGUF',
        'hf_filename': 'Mistral-Small-Instruct-2409-Q4_K_M.gguf',
        'context_length': 32768,
        'license': 'Apache 2.0',
        'languages': ['en', 'pl', 'fr', 'de', 'es', 'it', 'pt'],
        'use_cases': ['chat', 'code', 'reasoning', 'multilingual'],
    },
    # ── DeepSeek ───────────────────────────────────────────────────
    {
        'id': 'deepseek-coder-v2-lite-q4',
        'name': 'DeepSeek Coder V2 Lite 16B',
        'family': 'DeepSeek',
        'params': '16B (MoE)',
        'quant': 'Q4_K_M',
        'size_gb': 9.4,
        'ram_required_gb': 12,
        'vram_required_gb': 10,
        'description': 'DeepSeek Coder V2 Lite — MoE model, świetny w kodzie i matematyce.',
        'hf_repo': 'bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF',
        'hf_filename': 'DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'DeepSeek',
        'languages': ['en', 'zh'],
        'use_cases': ['code', 'math', 'reasoning'],
    },
    {
        'id': 'deepseek-r1-1.5b-q8',
        'name': 'DeepSeek R1 Distill Qwen 1.5B',
        'family': 'DeepSeek',
        'params': '1.5B',
        'quant': 'Q8_0',
        'size_gb': 1.6,
        'ram_required_gb': 3,
        'vram_required_gb': 2,
        'description': 'DeepSeek R1 1.5B — lekki model reasoning z chain-of-thought.',
        'hf_repo': 'bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF',
        'hf_filename': 'DeepSeek-R1-Distill-Qwen-1.5B-Q8_0.gguf',
        'context_length': 131072,
        'license': 'MIT',
        'languages': ['en', 'zh'],
        'use_cases': ['reasoning', 'math'],
    },
    {
        'id': 'deepseek-r1-7b-q4',
        'name': 'DeepSeek R1 Distill Qwen 7B',
        'family': 'DeepSeek',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.7,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'DeepSeek R1 7B — reasoning model, myśli krok po kroku.',
        'hf_repo': 'bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF',
        'hf_filename': 'DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'MIT',
        'languages': ['en', 'zh'],
        'use_cases': ['reasoning', 'math', 'code'],
    },
    {
        'id': 'deepseek-r1-14b-q4',
        'name': 'DeepSeek R1 Distill Qwen 14B',
        'family': 'DeepSeek',
        'params': '14B',
        'quant': 'Q4_K_M',
        'size_gb': 9.0,
        'ram_required_gb': 12,
        'vram_required_gb': 10,
        'description': 'DeepSeek R1 14B — mocny reasoning, zbliżona jakość do GPT-4o-mini.',
        'hf_repo': 'bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF',
        'hf_filename': 'DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf',
        'context_length': 131072,
        'license': 'MIT',
        'languages': ['en', 'zh'],
        'use_cases': ['reasoning', 'math', 'code'],
    },
    # ── StarCoder2 ─────────────────────────────────────────────────
    {
        'id': 'starcoder2-3b-q4',
        'name': 'StarCoder2 3B',
        'family': 'StarCoder',
        'params': '3B',
        'quant': 'Q4_K_M',
        'size_gb': 1.9,
        'ram_required_gb': 4,
        'vram_required_gb': 3,
        'description': 'BigCode StarCoder2 3B — specjalista od kodu, 600+ języków programowania.',
        'hf_repo': 'bartowski/starcoder2-3b-GGUF',
        'hf_filename': 'starcoder2-3b-Q4_K_M.gguf',
        'context_length': 16384,
        'license': 'BigCode OpenRAIL-M',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    {
        'id': 'starcoder2-7b-q4',
        'name': 'StarCoder2 7B',
        'family': 'StarCoder',
        'params': '7B',
        'quant': 'Q4_K_M',
        'size_gb': 4.4,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'StarCoder2 7B — mocniejszy model do generowania i analizy kodu.',
        'hf_repo': 'bartowski/starcoder2-7b-GGUF',
        'hf_filename': 'starcoder2-7b-Q4_K_M.gguf',
        'context_length': 16384,
        'license': 'BigCode OpenRAIL-M',
        'languages': ['en'],
        'use_cases': ['code'],
    },
    # ── Yi 1.5 ─────────────────────────────────────────────────────
    {
        'id': 'yi15-6b-q4',
        'name': 'Yi 1.5 6B Chat',
        'family': 'Yi',
        'params': '6B',
        'quant': 'Q4_K_M',
        'size_gb': 3.7,
        'ram_required_gb': 6,
        'vram_required_gb': 4,
        'description': '01.AI Yi 1.5 6B — chiński/angielski, dobry balans jakości i szybkości.',
        'hf_repo': 'bartowski/Yi-1.5-6B-Chat-GGUF',
        'hf_filename': 'Yi-1.5-6B-Chat-Q4_K_M.gguf',
        'context_length': 4096,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh'],
        'use_cases': ['chat', 'multilingual'],
    },
    {
        'id': 'yi15-9b-q4',
        'name': 'Yi 1.5 9B Chat',
        'family': 'Yi',
        'params': '9B',
        'quant': 'Q4_K_M',
        'size_gb': 5.5,
        'ram_required_gb': 8,
        'vram_required_gb': 6,
        'description': 'Yi 1.5 9B — jeden z najlepszych modeli open-source w swojej kategorii.',
        'hf_repo': 'bartowski/Yi-1.5-9B-Chat-GGUF',
        'hf_filename': 'Yi-1.5-9B-Chat-Q4_K_M.gguf',
        'context_length': 4096,
        'license': 'Apache 2.0',
        'languages': ['en', 'zh'],
        'use_cases': ['chat', 'reasoning', 'multilingual'],
    },
    # ── Nous Hermes 2 ──────────────────────────────────────────────
    {
        'id': 'hermes2-pro-8b-q4',
        'name': 'Nous Hermes 2 Pro 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q4_K_M',
        'size_gb': 4.9,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'Hermes 2 Pro — fine-tuned Llama 3, świetny do instrukcji i function calling.',
        'hf_repo': 'NousResearch/Hermes-2-Pro-Llama-3-8B-GGUF',
        'hf_filename': 'Hermes-2-Pro-Llama-3-8B-Q4_K_M.gguf',
        'context_length': 8192,
        'license': 'Llama 3 Community',
        'languages': ['en'],
        'use_cases': ['chat', 'code', 'reasoning'],
    },
    # ── OpenChat 3.6 ───────────────────────────────────────────────
    {
        'id': 'openchat-3.6-8b-q4',
        'name': 'OpenChat 3.6 8B',
        'family': 'Llama',
        'params': '8B',
        'quant': 'Q4_K_M',
        'size_gb': 4.9,
        'ram_required_gb': 7,
        'vram_required_gb': 5,
        'description': 'OpenChat 3.6 — zoptymalizowany pod konwersacje, bazujący na Llama 3.',
        'hf_repo': 'bartowski/openchat-3.6-8b-20240522-GGUF',
        'hf_filename': 'openchat-3.6-8b-20240522-Q4_K_M.gguf',
        'context_length': 8192,
        'license': 'Llama 3 Community',
        'languages': ['en'],
        'use_cases': ['chat'],
    },
    # ── Llama 3.3 ──────────────────────────────────────────────────
    {
        'id': 'llama33-70b-q2',
        'name': 'Llama 3.3 70B',
        'family': 'Llama',
        'params': '70B',
        'quant': 'IQ2_XS',
        'size_gb': 20.5,
        'ram_required_gb': 26,
        'vram_required_gb': 22,
        'description': 'Meta Llama 3.3 70B — najlepszy open-source model. ' +
                       'Ekstremalnie skompresowany (IQ2), wymaga dużo RAM.',
        'hf_repo': 'bartowski/Llama-3.3-70B-Instruct-GGUF',
        'hf_filename': 'Llama-3.3-70B-Instruct-IQ2_XS.gguf',
        'context_length': 131072,
        'license': 'Llama 3.3 Community',
        'languages': ['en', 'pl', 'de', 'fr', 'es'],
        'use_cases': ['chat', 'code', 'reasoning', 'math', 'multilingual', 'long-context'],
    },
]


def _detect_gpu_via_host():
    """Fallback GPU detection via host commands when GPUtil fails.
    Supports NVIDIA (nvidia-smi), Intel (lspci+i915), AMD (lspci+amdgpu)."""
    gpus = []

    # ── NVIDIA: nvidia-smi ──
    try:
        r = _host_run(
            "nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free "
            "--format=csv,noheader,nounits 2>/dev/null",
            timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 4:
                    gpus.append({
                        'name': parts[0],
                        'vram_total_gb': round(float(parts[1]) / 1024, 1),
                        'vram_used_gb': round(float(parts[2]) / 1024, 1),
                        'vram_free_gb': round(float(parts[3]) / 1024, 1),
                    })
    except Exception:
        pass
    if gpus:
        return gpus

    # ── Intel / AMD: lspci + kernel module check ──
    try:
        r = _host_run("lspci -nn 2>/dev/null", timeout=10)
        if r.returncode == 0 and r.stdout:
            import re as _re
            for line in r.stdout.splitlines():
                low = line.lower()
                if not any(k in low for k in ('vga', '3d controller', 'display controller')):
                    continue
                name_part = line.split(']: ')[-1] if ']: ' in line else line.split(':')[-1]
                name_part = _re.sub(r'\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]\s*$', '', name_part).strip()

                if 'intel' in low:
                    # Check i915 module is loaded
                    r2 = _host_run("lsmod 2>/dev/null | grep -q '^i915 '", timeout=5)
                    if r2.returncode == 0:
                        gpus.append({
                            'name': name_part,
                            'vram_total_gb': 0,
                            'vram_used_gb': 0,
                            'vram_free_gb': 0,
                            'type': 'intel_igpu',
                        })
                elif 'nvidia' in low:
                    # nvidia-smi failed above but card is present (no driver?)
                    gpus.append({
                        'name': name_part,
                        'vram_total_gb': 0,
                        'vram_used_gb': 0,
                        'vram_free_gb': 0,
                        'type': 'nvidia_no_driver',
                    })
                elif 'amd' in low or 'radeon' in low or 'advanced micro' in low:
                    r2 = _host_run("lsmod 2>/dev/null | grep -q '^amdgpu '", timeout=5)
                    if r2.returncode == 0:
                        gpus.append({
                            'name': name_part,
                            'vram_total_gb': 0,
                            'vram_used_gb': 0,
                            'vram_free_gb': 0,
                            'type': 'amd',
                        })
    except Exception:
        pass
    return gpus


def get_hardware_info():
    """Return system RAM and GPU VRAM in GB."""
    mem = psutil.virtual_memory()
    ram_total_gb = round(mem.total / 1073741824, 1)
    ram_available_gb = round(mem.available / 1073741824, 1)
    gpus = []
    vram_total_gb = 0
    # Try GPUtil first
    if _HAS_GPU:
        try:
            for gpu in GPUtil.getGPUs():
                vram_gb = round(gpu.memoryTotal / 1024, 1)
                gpus.append({
                    'name': gpu.name,
                    'vram_total_gb': vram_gb,
                    'vram_used_gb': round(gpu.memoryUsed / 1024, 1),
                    'vram_free_gb': round((gpu.memoryTotal - gpu.memoryUsed) / 1024, 1),
                })
                vram_total_gb += vram_gb
        except Exception:
            pass
    # Fallback: detect via nvidia-smi on host
    if not gpus:
        gpus = _detect_gpu_via_host()
        vram_total_gb = sum(g['vram_total_gb'] for g in gpus)
    return {
        'ram_total_gb': ram_total_gb,
        'ram_available_gb': ram_available_gb,
        'vram_total_gb': vram_total_gb,
        'gpus': gpus,
        'has_gpu': len(gpus) > 0,
    }


_CONFIG_FILE = data_path('model_library.json')
_DEFAULT_MODELS_PATH = data_path('models')
_download_lock = threading.Lock()
_download_state = {
    'active': False,
    'model_id': None,
    'progress': 0,
    'status': '',
    'error': None,
    'speed': '',
    'downloaded_bytes': 0,
    'total_bytes': 0,
}


def _fmt_bytes(n):
    """Human-readable byte size."""
    if n < 1024:
        return f'{n} B'
    for u in ('KB', 'MB', 'GB', 'TB'):
        n /= 1024
        if n < 1024:
            return f'{n:.1f} {u}'
    return f'{n:.1f} PB'


class _ProgressTqdm:
    """Minimal tqdm-compatible wrapper that feeds progress to emit_progress().

    huggingface_hub's _get_progress_bar_context calls:
        bar = tqdm_class(unit=..., total=N, initial=..., desc=..., disable=..., name=...)
        bar.update(chunk_bytes)
        bar.close()  (via context manager)
    """

    def __init__(self, *args, **kwargs):
        self.total = kwargs.get('total') or 0
        self.n = kwargs.get('initial') or 0
        self._emit = kwargs.pop('_emit_fn', None)
        self._filename = kwargs.pop('_filename', '') or kwargs.get('desc', '')
        self._last_emit = 0
        self._start = time.time()

    def update(self, n=1):
        self.n += n
        now = time.time()
        # Emit at most every 0.5s to avoid flooding
        if now - self._last_emit < 0.5:
            return
        self._last_emit = now
        elapsed = now - self._start
        pct = (self.n / self.total * 100) if self.total else 0
        speed = self.n / elapsed if elapsed > 1 else 0
        speed_str = _fmt_bytes(int(speed)) + '/s' if speed > 0 else ''
        status = (f'Pobieranie {self._filename}… '
                  f'{_fmt_bytes(self.n)} / {_fmt_bytes(self.total)}')
        if self._emit:
            self._emit(min(pct, 99.5), status, speed_str)

    def close(self):
        pass

    def set_description(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ModelLibrary:
    """Manages the model catalog, hardware recommendations, downloads, and storage."""

    def __init__(self):
        self.catalog = list(MODEL_CATALOG)
        self._config = self._load_config()

    def _load_config(self):
        defaults = {
            'models_path': _DEFAULT_MODELS_PATH,
            'downloaded': {},
            'custom_models': [],
            'active_model_id': None,
        }
        try:
            if os.path.isfile(_CONFIG_FILE):
                with open(_CONFIG_FILE, 'r') as f:
                    saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
        return defaults

    def _save_config(self):
        try:
            with open(_CONFIG_FILE, 'w') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f'  [model_library] Error saving config: {e}')

    @property
    def models_path(self):
        return self._config.get('models_path', _DEFAULT_MODELS_PATH)

    def set_models_path(self, path):
        """Set models storage directory. Returns (ok, error_message)."""
        path = os.path.realpath(path)
        try:
            os.makedirs(path, exist_ok=True)
            test_file = os.path.join(path, '.ethos_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except OSError as e:
            return (False, f'Nie można zapisywać w {path}: {e}')
        self._config['models_path'] = path
        self._save_config()
        return (True, None)

    def get_disk_space(self, path=None):
        """Get free disk space at models_path (or given path) in GB."""
        try:
            target = path or self.models_path
            usage = shutil.disk_usage(target)
            return {
                'total_gb': round(usage.total / 1073741824, 1),
                'free_gb': round(usage.free / 1073741824, 1),
                'used_gb': round(usage.used / 1073741824, 1),
            }
        except Exception:
            return {'total_gb': 0, 'free_gb': 0, 'used_gb': 0}

    def get_all_models(self):
        """Return catalog + custom models, all enriched with status."""
        all_models = self.catalog + self._config.get('custom_models', [])
        return all_models

    def get_model(self, model_id):
        """Get a single model by ID."""
        for m in self.get_all_models():
            if m['id'] == model_id:
                return m
        return None

    def get_recommendations(self, hw=None):
        """Filter and annotate models for current hardware.

        Returns list of models with added fields:
          - status: 'recommended' | 'possible' | 'too_heavy'
          - status_label: human-readable Polish label
          - downloaded: bool
          - active: bool
        """
        if hw is None:
            hw = get_hardware_info()

        ram_gb = hw['ram_total_gb']
        vram_gb = hw['vram_total_gb']
        downloaded = self._config.get('downloaded', {})
        active_id = self._config.get('active_model_id')

        result = []
        for m in self.get_all_models():
            entry = dict(m)
            req_ram = m.get('ram_required_gb', 99)
            req_vram = m.get('vram_required_gb', 99)

            fits_ram = ram_gb >= req_ram
            fits_vram = (vram_gb > 0 and vram_gb >= req_vram) or True
            has_margin = ram_gb >= req_ram * 1.2

            if fits_ram and has_margin:
                entry['status'] = 'recommended'
                entry['status_label'] = 'Zalecany'
            elif fits_ram:
                entry['status'] = 'possible'
                entry['status_label'] = 'Może działać wolno'
            else:
                entry['status'] = 'too_heavy'
                entry['status_label'] = 'Niezalecany — za mało RAM'

            if vram_gb > 0:
                if fits_vram:
                    entry['gpu_status'] = 'gpu_ok'
                    entry['gpu_label'] = f'GPU OK ({vram_gb:.0f} GB VRAM)'
                else:
                    entry['gpu_status'] = 'gpu_partial'
                    entry['gpu_label'] = 'CPU+GPU offload'
            elif hw.get('has_gpu'):
                # iGPU (e.g. Intel/AMD) with shared RAM — can still accelerate
                gpu_name = hw['gpus'][0]['name'] if hw.get('gpus') else 'iGPU'
                gpu_type = hw['gpus'][0].get('type', '') if hw.get('gpus') else ''
                if 'intel' in gpu_type or 'igpu' in gpu_type:
                    entry['gpu_status'] = 'igpu'
                    entry['gpu_label'] = f'Intel iGPU (RAM współdzielony)'
                else:
                    entry['gpu_status'] = 'gpu_shared'
                    entry['gpu_label'] = f'GPU (RAM współdzielony)'
            else:
                entry['gpu_status'] = 'cpu_only'
                entry['gpu_label'] = 'Tylko CPU'

            dl = downloaded.get(m['id'])
            entry['downloaded'] = dl is not None

            if dl:
                entry['local_path'] = dl.get('path', '')
                entry['downloaded_at'] = dl.get('downloaded_at', '')

                if not os.path.isfile(entry['local_path']):
                    entry['downloaded'] = False
                    entry['local_path'] = ''

            entry['active'] = m['id'] == active_id
            result.append(entry)

        order = {'recommended': 0, 'possible': 1, 'too_heavy': 2}
        result.sort(key=lambda x: (order.get(x['status'], 9), not x['downloaded'], x.get('size_gb', 0)))

        return result

    def check_before_download(self, model_id):
        """Pre-download checks. Returns (ok, error_message)."""
        model = self.get_model(model_id)
        if not model:
            return (False, 'Model nieznaleziony w katalogu')
        if not _HAS_HF:
            return (False, 'Biblioteka huggingface_hub nie jest zainstalowana. Uruchom: pip install huggingface_hub')
        disk = self.get_disk_space()
        required_gb = model.get('size_gb', 0) * 1.1
        if disk['free_gb'] < required_gb:
            return (False, f"Za mało miejsca na dysku. Wymagane: {required_gb:.1f} GB, dostępne: {disk['free_gb']:.1f} GB w {self.models_path}")
        try:
            os.makedirs(self.models_path, exist_ok=True)
        except OSError as e:
            return (False, f'Nie można utworzyć katalogu modeli: {e}')
        return (True, None)

    def start_download(self, model_id, socketio=None):
        """Start model download in background. Returns (ok, error_message)."""
        with _download_lock:
            if _download_state['active']:
                return (False, 'Inne pobieranie jest w toku')

        ok, err = self.check_before_download(model_id)
        if not ok:
            return (False, err)

        model = self.get_model(model_id)

        with _download_lock:
            _download_state['active'] = True
            _download_state['model_id'] = model_id
            _download_state['progress'] = 0
            _download_state['status'] = 'Rozpoczynanie pobierania…'
            _download_state['error'] = None
            _download_state['speed'] = ''
            _download_state['downloaded_bytes'] = 0
            _download_state['total_bytes'] = 0

        thread = threading.Thread(
            target=self._download_thread,
            args=(model, socketio),
            daemon=True,
        )
        thread.start()
        return (True, None)

    def _download_thread(self, model, socketio=None):
        """Background download thread."""
        model_id = model['id']
        hf_repo = model['hf_repo']
        hf_file = model['hf_filename']

        def emit_progress(pct, status, speed=''):
            with _download_lock:
                _download_state['progress'] = pct
                _download_state['status'] = status
                _download_state['speed'] = speed
            if socketio:
                socketio.emit('model_download_progress', {
                    'model_id': model_id,
                    'progress': pct,
                    'status': status,
                    'speed': speed,
                })

        try:
            emit_progress(0, f'Pobieranie {hf_file}…')

            target_path = os.path.join(self.models_path, hf_file)

            # Build a factory that injects our emit_progress into _ProgressTqdm
            def _tqdm_factory(*args, **kwargs):
                kwargs['_emit_fn'] = emit_progress
                kwargs['_filename'] = hf_file
                return _ProgressTqdm(*args, **kwargs)

            downloaded_path = hf_hub_download(
                repo_id=hf_repo,
                filename=hf_file,
                local_dir=self.models_path,
                local_dir_use_symlinks=False,
                tqdm_class=_tqdm_factory,
            )

            if not os.path.isfile(downloaded_path):
                if os.path.isfile(target_path):
                    downloaded_path = target_path
                else:
                    raise FileNotFoundError(f'Plik nie znaleziony po pobraniu: {hf_file}')

            file_size = os.path.getsize(downloaded_path)
            size_gb = round(file_size / 1073741824, 2)

            self._config.setdefault('downloaded', {})[model_id] = {
                'filename': hf_file,
                'path': downloaded_path,
                'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'size_gb': size_gb,
            }
            self._save_config()

            emit_progress(100, 'Pobrano pomyślnie')

            if socketio:
                socketio.emit('model_download_complete', {
                    'model_id': model_id,
                    'path': downloaded_path,
                    'size_gb': size_gb,
                })
        except Exception as e:
            with _download_lock:
                _download_state['error'] = str(e)
                _download_state['status'] = f'Błąd: {e}'
            if socketio:
                socketio.emit('model_download_error', {
                    'model_id': model_id,
                    'error': str(e),
                })
        finally:
            with _download_lock:
                _download_state['active'] = False

    def get_download_status(self):
        """Return current download state."""
        with _download_lock:
            return dict(_download_state)

    def cancel_download(self):
        """Cancel is not directly supported by hf_hub_download;
        mark as cancelled so UI can reflect."""
        with _download_lock:
            _download_state['active'] = False
            _download_state['status'] = 'Anulowano'
            _download_state['error'] = 'Pobieranie anulowane'

    def delete_model(self, model_id):
        """Delete a downloaded model file. Returns (ok, error_msg)."""
        downloaded = self._config.get('downloaded', {})
        dl = downloaded.get(model_id)
        if not dl:
            return (False, 'Model nie jest pobrany')
        path = dl.get('path', '')
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                return (False, f'Nie można usunąć pliku: {e}')
        del downloaded[model_id]
        if self._config.get('active_model_id') == model_id:
            self._config['active_model_id'] = None
        self._save_config()
        return (True, None)

    def set_active_model(self, model_id):
        """Set which model is currently active/loaded."""
        if model_id is not None:
            dl = self._config.get('downloaded', {}).get(model_id)
            if not dl:
                return (False, 'Model nie jest pobrany')
            if not os.path.isfile(dl.get('path', '')):
                return (False, 'Plik modelu nie istnieje na dysku')
        # Unload previous model if switching
        if model_id != self._config.get('active_model_id'):
            self.unload_model()
        self._config['active_model_id'] = model_id
        self._save_config()
        return (True, None)

    def get_active_model(self):
        """Return active model info or None."""
        active_id = self._config.get('active_model_id')
        if not active_id:
            return None
        model = self.get_model(active_id)
        if not model:
            return None
        dl = self._config.get('downloaded', {}).get(active_id)
        if not dl or not os.path.isfile(dl.get('path', '')):
            return None
        info = dict(model)
        info['local_path'] = dl['path']
        info['loaded'] = self._loaded_model is not None and self._loaded_model_id == active_id
        return info

    # ── Local model loading (llama-cpp-python) ──────────────────────

    _loaded_model = None
    _loaded_model_id = None
    _model_lock = threading.Lock()       # guards _loaded_model / _loaded_model_id
    _last_used = 0.0
    _idle_timeout = 30 * 60  # 30 minutes
    _idle_timer = None

    # Background loading state (readable by chat handler for progress SSE)
    _loading_in_progress = False
    _loading_model_id = None
    _loading_start = 0.0

    def is_loading(self):
        """Return True if a model is currently being loaded in the background."""
        return ModelLibrary._loading_in_progress

    def loading_info(self):
        """Return info about the current background model load, or None."""
        if not ModelLibrary._loading_in_progress:
            return None
        elapsed = time.time() - ModelLibrary._loading_start
        return {
            'model_id': ModelLibrary._loading_model_id,
            'elapsed_s': round(elapsed, 1),
        }

    def load_model(self, model_id=None):
        """Load a GGUF model into memory.

        Returns (llama_instance, error_msg).
        Uses gevent's native threadpool (real OS threads) so the Llama()
        C constructor doesn't block the event loop.
        """
        if model_id is None:
            model_id = self._config.get('active_model_id')
        if not model_id:
            return (None, 'Brak aktywnego modelu')

        with self._model_lock:
            # Already loaded
            if self._loaded_model is not None and self._loaded_model_id == model_id:
                return (self._loaded_model, None)

            # Unload previous
            self._unload_locked()

            dl = self._config.get('downloaded', {}).get(model_id)
            if not dl:
                return (None, 'Model nie jest pobrany')
            path = dl.get('path', '')
            if not os.path.isfile(path):
                return (None, 'Plik modelu nie istnieje na dysku')

            # RAM guard: check if enough memory is available
            try:
                file_size_gb = os.path.getsize(path) / (1024**3)
                needed_gb = file_size_gb * 1.1
                mem = psutil.virtual_memory()
                available_gb = mem.available / (1024**3)
                total_gb = mem.total / (1024**3)
                usable_gb = max(available_gb, total_gb * 0.85)
                min_free_gb = 0.5
                if usable_gb - needed_gb < min_free_gb:
                    return (None,
                            f'Za mało pamięci RAM. Model wymaga ~{needed_gb:.1f} GB, '
                            f'dostępne: {available_gb:.1f} GB (minimum {min_free_gb} GB musi pozostać wolne). '
                            f'Zamknij inne aplikacje lub wybierz mniejszy model.')
            except Exception:
                pass

            try:
                from llama_cpp import Llama
            except ImportError:
                return (None, 'llama-cpp-python nie jest zainstalowany')

            try:
                # GPU layers — only if llama-cpp has a real GPU backend
                n_gpu_layers = 0
                try:
                    from llama_cpp import llama_cpp as _lc
                    _gpu_offload_ok = _lc.llama_supports_gpu_offload()
                except Exception:
                    _gpu_offload_ok = False

                if _gpu_offload_ok:
                    try:
                        import GPUtil
                        if GPUtil.getGPUs():
                            n_gpu_layers = -1
                    except Exception:
                        pass
                    if n_gpu_layers == 0:
                        try:
                            _fb_gpus = _detect_gpu_via_host()
                            if any(g.get('vram_total_gb', 0) > 0 for g in _fb_gpus):
                                n_gpu_layers = -1
                        except Exception:
                            pass

                model_meta = self.get_model(model_id) or {}
                # ── N150-tuned parameters ──
                # ctx_size: 2048 keeps prefill fast on 4-core Intel
                # n_threads: 3 = leave 1 core free for Flask/gevent/system
                # n_batch: 256 = Intel-friendly prefill batch size
                phys_cores = psutil.cpu_count(logical=False) or 2
                n_threads = max(1, phys_cores - 1)   # 3 on N150
                ctx_size = min(model_meta.get('context_length', 4096), 2048)
                n_batch = 256   # speeds up prompt processing on Intel CPUs

                print(f"[ai-local] Loading model {model_id} from {path} "
                      f"(ctx={ctx_size}, n_threads={n_threads}, n_batch={n_batch}, "
                      f"gpu_layers={n_gpu_layers}, "
                      f"gpu_offload_supported={_gpu_offload_ok})", flush=True)

                # ── Load in a REAL OS thread via gevent threadpool ──
                # monkey.patch_all() turns threading.Thread into greenlets,
                # but Llama() is a C extension that holds the GIL and never
                # yields — freezing the entire event loop.  The threadpool
                # uses native OS threads, so ctypes releases the GIL during
                # the C call and the event loop stays responsive.
                ModelLibrary._loading_in_progress = True
                ModelLibrary._loading_model_id = model_id
                ModelLibrary._loading_start = time.time()

                def _do_load():
                    return Llama(
                        model_path=path,
                        n_ctx=ctx_size,
                        n_gpu_layers=n_gpu_layers,
                        n_threads=n_threads,
                        n_batch=n_batch,
                        verbose=False,
                    )

                try:
                    import gevent
                    hub = gevent.get_hub()
                    async_result = hub.threadpool.spawn(_do_load)
                    # Cooperative wait — yields to other greenlets!
                    llm = async_result.get(timeout=600)
                except gevent.Timeout:
                    ModelLibrary._loading_in_progress = False
                    print("[ai-local] Model loading timed out (>10 min)", flush=True)
                    return (None, 'Ładowanie modelu trwa zbyt długo (>10 min). '
                                  'Spróbuj mniejszy model.')
                except Exception as ex:
                    ModelLibrary._loading_in_progress = False
                    return (None, f'Błąd ładowania modelu: {ex}')
                finally:
                    ModelLibrary._loading_in_progress = False

                ModelLibrary._loaded_model = llm
                ModelLibrary._loaded_model_id = model_id
                ModelLibrary._last_used = time.time()
                self._start_idle_timer()
                elapsed = time.time() - ModelLibrary._loading_start
                print(f"[ai-local] Model {model_id} loaded successfully "
                      f"({elapsed:.1f}s)", flush=True)
                return (llm, None)
            except Exception as ex:
                ModelLibrary._loading_in_progress = False
                return (None, f'Błąd ładowania modelu: {ex}')

    def unload_model(self):
        """Unload the currently loaded model from memory."""
        with self._model_lock:
            self._unload_locked()

    def _unload_locked(self):
        """Internal unload (must hold _model_lock)."""
        if ModelLibrary._loaded_model is not None:
            mid = ModelLibrary._loaded_model_id
            try:
                del ModelLibrary._loaded_model
            except Exception:
                pass
            ModelLibrary._loaded_model = None
            ModelLibrary._loaded_model_id = None
            print(f"[ai-local] Model {mid} unloaded", flush=True)

    def get_loaded_model(self):
        """Return (llama_instance, model_id) or (None, None) if no model loaded."""
        return (ModelLibrary._loaded_model, ModelLibrary._loaded_model_id)

    def touch_model(self):
        """Mark model as recently used — resets idle unload timer."""
        ModelLibrary._last_used = time.time()
        self._start_idle_timer()

    def _start_idle_timer(self):
        """Start/restart the idle unload timer."""
        if ModelLibrary._idle_timer is not None:
            ModelLibrary._idle_timer.cancel()
        t = threading.Timer(self._idle_timeout, self._idle_unload)
        t.daemon = True
        t.start()
        ModelLibrary._idle_timer = t

    def _idle_unload(self):
        """Called by timer — unload model if still idle."""
        elapsed = time.time() - ModelLibrary._last_used
        if elapsed >= self._idle_timeout and ModelLibrary._loaded_model is not None:
            print(f"[ai-local] Auto-unloading model after {int(elapsed)}s idle", flush=True)
            self.unload_model()

    # ── Benchmark ──────────────────────────────────────────────────

    _benchmark_lock = threading.Lock()
    _benchmark_running = False

    def run_benchmark(self, model_id=None, prompt=None):
        """Run an inference benchmark measuring TPS and TTFT.

        Returns dict with: tps, ttft, tokens_generated, total_time_s, tier, model_id
        """
        if ModelLibrary._benchmark_running:
            return {'error': 'Benchmark jest już uruchomiony'}

        with self._benchmark_lock:
            ModelLibrary._benchmark_running = True

        try:
            llm, err = self.load_model(model_id)
            if err:
                return {'error': err}

            if prompt is None:
                prompt = 'Explain quantum computing in 3 sentences.'

            messages = [
                {'role': 'system', 'content': 'You are a helpful assistant. Respond concisely.'},
                {'role': 'user', 'content': prompt},
            ]

            # Warm up — small generation to prime caches
            try:
                llm.create_chat_completion(
                    messages=[{'role': 'user', 'content': 'Hi'}],
                    max_tokens=5,
                    temperature=0.0,
                )
            except Exception:
                pass

            # Actual benchmark
            start = time.time()
            ttft = None
            n_tokens = 0
            total_text = ''

            try:
                response = llm.create_chat_completion(
                    messages=messages,
                    max_tokens=128,
                    temperature=0.7,
                    stream=True,
                )
                for chunk in response:
                    delta = chunk.get('choices', [{}])[0].get('delta', {})
                    content = delta.get('content', '')
                    if content:
                        if ttft is None:
                            ttft = time.time() - start
                        n_tokens += 1
                        total_text += content
            except Exception as ex:
                return {'error': f'Błąd inferencji podczas benchmarku: {ex}'}

            total_time = time.time() - start
            if n_tokens == 0:
                return {'error': 'Model nie wygenerował żadnych tokenów'}

            tps = n_tokens / total_time if total_time > 0 else 0
            if ttft is None:
                ttft = total_time

            self.touch_model()

            target_id = model_id or self._config.get('active_model_id')
            result = {
                'model_id': target_id,
                'tps': round(tps, 1),
                'ttft': round(ttft, 3),
                'tokens_generated': n_tokens,
                'total_time_s': round(total_time, 2),
                'tier': get_tier(tps),
                'sample_text': total_text[:200],
            }

            # Persist benchmark results
            self._config.setdefault('benchmarks', {})[target_id] = {
                'tps': result['tps'],
                'ttft': result['ttft'],
                'tier_id': result['tier']['id'],
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            }
            self._save_config()

            return result

        finally:
            with self._benchmark_lock:
                ModelLibrary._benchmark_running = False

    def get_last_benchmark(self, model_id=None):
        """Get last benchmark result for a model."""
        target = model_id or self._config.get('active_model_id')
        if not target:
            return None
        return self._config.get('benchmarks', {}).get(target)

    def get_health_score(self):
        """Compute AI Health Score (0-100) based on system state."""
        score = 0
        details = []

        # 1. Dependencies (20 pts)
        try:
            import llama_cpp  # noqa: F401
            score += 20
            details.append({'key': 'deps', 'label': 'Zależności', 'score': 20, 'max': 20, 'ok': True})
        except ImportError:
            details.append({'key': 'deps', 'label': 'Zależności', 'score': 0, 'max': 20, 'ok': False,
                            'hint': 'Zainstaluj llama-cpp-python'})

        # 2. Active model (20 pts)
        active = self.get_active_model()
        if active:
            score += 20
            details.append({'key': 'model', 'label': 'Aktywny model', 'score': 20, 'max': 20, 'ok': True,
                            'hint': active.get('name', '')})
        else:
            details.append({'key': 'model', 'label': 'Aktywny model', 'score': 0, 'max': 20, 'ok': False,
                            'hint': 'Pobierz i aktywuj model'})

        # 3. Model loaded in RAM (15 pts)
        loaded, _ = self.get_loaded_model()
        if loaded is not None:
            score += 15
            details.append({'key': 'loaded', 'label': 'Model w pamięci', 'score': 15, 'max': 15, 'ok': True})
        else:
            details.append({'key': 'loaded', 'label': 'Model w pamięci', 'score': 0, 'max': 15, 'ok': False,
                            'hint': 'Model auto-ładuje się przy pierwszym czacie'})

        # 4. RAM headroom (20 pts)
        mem = psutil.virtual_memory()
        available_pct = 100 - mem.percent
        if available_pct >= 30:
            ram_score = 20
        elif available_pct >= 15:
            ram_score = 10
        else:
            ram_score = 0
        score += ram_score
        details.append({
            'key': 'ram', 'label': 'Pamięć RAM',
            'score': ram_score, 'max': 20, 'ok': ram_score >= 10,
            'hint': f'{available_pct:.0f}% wolne'
        })

        # 5. Benchmark / TPS (15 pts)
        bench = self.get_last_benchmark()
        if bench:
            tps = bench.get('tps', 0)
            if tps >= 10:
                bench_score = 15
            elif tps >= 5:
                bench_score = 10
            elif tps > 0:
                bench_score = 5
            else:
                bench_score = 0
            score += bench_score
            details.append({
                'key': 'perf', 'label': 'Wydajność',
                'score': bench_score, 'max': 15, 'ok': bench_score >= 10,
                'hint': f'{tps} tok/s'
            })
        else:
            details.append({
                'key': 'perf', 'label': 'Wydajność',
                'score': 0, 'max': 15, 'ok': False,
                'hint': 'Uruchom benchmark'
            })

        # 6. Disk space (10 pts)
        disk = self.get_disk_space()
        if disk['free_gb'] >= 10:
            disk_score = 10
        elif disk['free_gb'] >= 5:
            disk_score = 5
        else:
            disk_score = 0
        score += disk_score
        details.append({
            'key': 'disk', 'label': 'Dysk',
            'score': disk_score, 'max': 10, 'ok': disk_score >= 5,
            'hint': f'{disk["free_gb"]} GB wolne'
        })

        return {
            'score': min(score, 100),
            'max_score': 100,
            'grade': 'A' if score >= 85 else 'B' if score >= 65 else 'C' if score >= 40 else 'D',
            'details': details,
            'local_processing': True,  # Always true — everything runs locally
        }

    def get_recommended_model(self, hw=None):
        """Auto-select the best model for current hardware."""
        if hw is None:
            hw = get_full_hardware_info()
        ram = hw.get('ram_total_gb', 0)
        recs = self.get_recommendations(hw)
        # Pick best recommended model that fits
        for m in recs:
            if m['status'] == 'recommended' and m.get('downloaded'):
                return m
        # Fallback: any recommended
        for m in recs:
            if m['status'] == 'recommended':
                return m
        # Fallback: any possible
        for m in recs:
            if m['status'] == 'possible':
                return m
        return recs[0] if recs else None

    def add_custom_model(self, hf_url):
        """Add a custom model by HF URL or repo/file spec.

        Accepts:
          - https://huggingface.co/USER/REPO/blob/main/FILE.gguf
          - USER/REPO/FILE.gguf
          - USER/REPO (will need filename later)

        Returns (model_entry, error_msg).
        """
        hf_url = hf_url.strip()
        repo = ''
        filename = ''

        if 'huggingface.co/' in hf_url:
            import re
            m = re.search(
                r'huggingface\.co/([^/]+/[^/]+)(?:/(?:blob|resolve)/[^/]+/(.+?))?(?:\?|$)',
                hf_url,
            )
            if m:
                repo = m.group(1)
                filename = m.group(2) or ''
            else:
                return (None, 'Nie można sparsować URL-a Hugging Face')
        elif '/' in hf_url:
            parts = hf_url.split('/')
            if len(parts) >= 3:
                repo = parts[0] + '/' + parts[1]
                filename = '/'.join(parts[2:])
            elif len(parts) == 2:
                repo = hf_url
            else:
                return (None, 'Nieprawidłowy format. Użyj: USER/REPO/PLIK.gguf')
        else:
            return (None, 'Nieprawidłowy format. Podaj URL lub USER/REPO/PLIK.gguf')

        if not repo:
            return (None, 'Nie znaleziono nazwy repozytorium')

        if not filename:
            if _HAS_HF:
                try:
                    api = HfApi()
                    files = api.list_repo_files(repo)
                    gguf_files = [f for f in files if f.endswith('.gguf')]
                    if not gguf_files:
                        return (None, f'Brak plików .gguf w repozytorium {repo}')
                    filename = next((f for f in gguf_files if 'Q4_K_M' in f), gguf_files[0])
                except Exception as e:
                    return (None, f'Nie można pobrać listy plików z {repo}: {e}')
            else:
                return (None, 'Podaj pełną ścieżkę z nazwą pliku (huggingface_hub nie zainstalowany)')

        model_id = f"custom_{repo.replace('/', '_')}_{filename.replace('/', '_').replace('.', '_')}"

        entry = {
            'id': model_id,
            'name': filename.replace('.gguf', ''),
            'family': 'Custom',
            'params': '?',
            'quant': _guess_quant(filename),
            'size_gb': 0,
            'ram_required_gb': 0,
            'vram_required_gb': 0,
            'description': f'Niestandardowy model z {repo}',
            'hf_repo': repo,
            'hf_filename': filename,
            'context_length': 0,
            'license': '?',
            'languages': [],
            'use_cases': [],
            'custom': True,
        }

        customs = self._config.get('custom_models', [])
        if any(c['id'] == model_id for c in customs):
            return (None, 'Ten model jest już dodany')

        customs.append(entry)
        self._config['custom_models'] = customs
        self._save_config()
        return (entry, None)

    def remove_custom_model(self, model_id):
        """Remove a custom model from the catalog (and delete file if downloaded)."""
        customs = self._config.get('custom_models', [])
        found = next((c for c in customs if c['id'] == model_id), None)
        if not found:
            return (False, 'Model niestandardowy nie znaleziony')

        self.delete_model(model_id)
        self._config['custom_models'] = [c for c in customs if c['id'] != model_id]
        self._save_config()
        return (True, None)


def _guess_quant(filename):
    """Guess quantization level from filename."""
    fn = filename.upper()
    for q in ('Q2_K', 'Q3_K_S', 'Q3_K_M', 'Q3_K_L', 'Q4_0', 'Q4_K_S', 'Q4_K_M',
              'Q5_0', 'Q5_K_S', 'Q5_K_M', 'Q6_K', 'Q8_0', 'F16', 'F32'):
        if q in fn:
            return q
    return '?'


_library_instance = None


def get_library():
    """Return singleton ModelLibrary instance."""
    global _library_instance
    if _library_instance is None:
        _library_instance = ModelLibrary()
    return _library_instance
