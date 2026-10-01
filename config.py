import os


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


QWEN_KEEP_ALIVE = os.getenv("QWEN_KEEP_ALIVE", "30m")
QWEN_MAX_TOKENS = _int("QWEN_MAX_TOKENS", 260)
QWEN_CONTEXT_TOP_K = _int("QWEN_CONTEXT_TOP_K", 3)
QWEN_CONTEXT_CHARS = _int("QWEN_CONTEXT_CHARS", 1400)
QWEN_TIMEOUT = _float("QWEN_TIMEOUT", 120.0)
SEARCH_TOP_K = _int("SEARCH_TOP_K", 5)
SEARCH_CONFIDENCE_THRESHOLD = _float("SEARCH_CONFIDENCE_THRESHOLD", 0.50)
SEARCH_CACHE_TTL = _float("SEARCH_CACHE_TTL", 300.0)
SEARCH_CACHE_SIZE = _int("SEARCH_CACHE_SIZE", 128)
ACTIVE_PROJECT_SEARCH_THRESHOLD = _float("ACTIVE_PROJECT_SEARCH_THRESHOLD", 0.48)
ACTIVE_PROJECT_SELECTION_MARGIN = _float("ACTIVE_PROJECT_SELECTION_MARGIN", 0.08)
