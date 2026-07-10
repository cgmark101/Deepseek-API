"""Server configuration: the OpenAI-facing model names and what they map to."""

import os

# Requests per minute allowed per client IP (override with RATE_LIMIT_PER_MINUTE).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

# When the server has no session, should it pop a visible browser window for
# interactive sign-in (the first request then blocks until you finish logging
# in)? On by default for local single-user use. Set to "0"/"false" for headless
# deployments, where it instead returns a 503 telling the caller to run
# `python -m deepseek.auth`.
SERVER_INTERACTIVE_LOGIN = os.getenv("SERVER_INTERACTIVE_LOGIN", "1").lower() not in (
    "0", "false", "no", "off",
)

CLEANUP_EPHEMERAL_CHATS = os.getenv("CLEANUP_EPHEMERAL_CHATS", "1").lower() not in (
    "0", "false", "no", "off",
)

# Public model ids the server advertises (via /v1/models) and accepts, mapped to
# DeepSeek's `model_type` wire value. This is the MODEL axis ONLY — it picks
# which model answers. DeepThink and web Search are orthogonal tools requested
# per call via `tool_names` (see deepseek.client.KNOWN_TOOLS), never encoded in
# the model name.
#
# "vision" is deferred: it only does anything with an image attached, which needs
# ref_file_ids / file-upload plumbing we don't have yet.
VIRTUAL_MODELS = {
    "deepseek-chat":           {"model_type": "default", "thinking": False, "search": False},
    "deepseek-expert":         {"model_type": "expert",  "thinking": False, "search": False},
    
    # DeepThink (Reasoning) virtual models
    "deepseek-reasoner":       {"model_type": "default", "thinking": True,  "search": False},
    "deepseek-chat-think":     {"model_type": "default", "thinking": True,  "search": False},
    "deepseek-expert-think":   {"model_type": "expert",  "thinking": True,  "search": False},
    
    # Web Search virtual models
    "deepseek-chat-search":    {"model_type": "default", "thinking": False, "search": True},
    "deepseek-expert-search":  {"model_type": "expert",  "thinking": False, "search": True},
    
    # Combined virtual models
    "deepseek-expert-all":     {"model_type": "expert",  "thinking": True,  "search": True},
}

MODEL_MAP = {k: v["model_type"] for k, v in VIRTUAL_MODELS.items()}

DEFAULT_MODEL = "deepseek-chat"


def is_known_model(name: str) -> bool:
    """Whether `name` is a model id we accept (used to 404 unknown models)."""
    return name in MODEL_MAP


def resolve_model_type(name: str) -> str:
    """Translate a public model id to DeepSeek's `model_type` wire value.

    Caller must check `is_known_model` first; this raises KeyError otherwise.
    """
    return MODEL_MAP[name]


def resolve_virtual_model(name: str) -> dict:
    """Translate a public model id to its virtual model settings (model_type, thinking, search)."""
    return VIRTUAL_MODELS.get(name, {"model_type": "default", "thinking": False, "search": False})
