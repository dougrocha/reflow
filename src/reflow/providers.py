"""The AI providers a conversion may ask, their default models and prices, and the default
budgets of each AI step.

Kept in the base layer, apart from the passes that call the providers (reflow.analyze.enrich,
reflow.analyze.label_assist and the assists that share its model), so that reflow.run can
validate its options without importing the analysis, and the CLI and the scripts read the same
defaults the options do.
"""

ALT_TEXT_PROVIDERS = ("none", "local", "ollama", "anthropic")   # --alt-text
ASSIST_PROVIDERS = ("ollama", "anthropic")   # --assist-provider: the form assists share one
DEFAULT_ASSIST_PROVIDER = "ollama"
DIRECT_PROMPTS = ("base", "complete", "forms", "example")   # --direct-prompt; analyze.direct.PROMPTS holds their texts

CLAUDE_HAIKU = "claude-haiku-5-5"
CLAUDE_SONNET = "claude-sonnet-5-5"
CLAUDE_OPUS = "claude-opus-5-5"

# Figure description needs a vision-language model; the form assists' default (gemma4:e4b) is
# not one and stays theirs.
ALT_TEXT_MODELS = {"local": "Qwen/Qwen3-VL-8B-Instruct",   # Apache-2.0
                   "ollama": "qwen3-vl:8b",
                   "anthropic": CLAUDE_HAIKU}
# The default of the form assists (labels, structure, scan fields) for each provider.
ASSIST_MODELS = {"ollama": "gemma4:e4b", "anthropic": CLAUDE_HAIKU}
PAGE_VISION_MODEL = "qwen3-vl:8b"
TRANSCRIPTION_MODEL = "qwen3-vl:8b"   # --assist local, through Ollama
FORM_REPAIR_MODEL = "qwen3-vl:8b"
DIRECT_MODEL = CLAUDE_HAIKU
POLISH_MODEL = CLAUDE_HAIKU

# Dollars per million tokens, in and out (Anthropic's list prices, 2026-10; prompts up to 100K
# tokens). A model not here costs nothing: a local one.
PRICES: dict[str, tuple[float, float]] = {
    CLAUDE_HAIKU: (0.10, 0.50),
    CLAUDE_SONNET: (2.00, 10.00),
    CLAUDE_OPUS: (4.00, 20.00),
}

# The most an AI step asks per document by default, and the most a run may spend.
LABEL_MAX_CALLS = 25            # uncached questions
STRUCTURE_MAX_CALLS = 25
SCAN_FIELDS_MAX_CALLS = 25
PAGE_VISION_MAX_PAGES = 100     # uncached pages
FORM_REPAIR_MAX_CALLS = 24      # uncached calls, rereads included
DIRECT_MAX_DOLLARS = 5.0
POLISH_MAX_DOLLARS = 5.0


def provider_of(model: str) -> str:
    """The provider a model name runs on: a Claude model on the Anthropic API, any other on the
    local Ollama server."""
    return "anthropic" if model.startswith("claude-") else "ollama"


def resolve_model(provider: str, model: str | None) -> str:
    """The assist model id for `provider`: the one asked for, else that provider's default."""
    return (model or "").strip() or ASSIST_MODELS[provider]
