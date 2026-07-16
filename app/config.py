"""
Central configuration. Everything is read from environment variables so the
same code runs locally, in the demo, and in CI without edits.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- LLM backend -----------------------------------------------------------
# Set LLM_BACKEND=mock to run the whole pipeline with a free, deterministic
# rule-based stand-in for the model (no API key needed). This is what the
# eval harness uses as the "before tuning" baseline, and it's also handy for
# developing the UI/eval code without burning API credits.
#
# Set LLM_BACKEND=openrouter to hit the real Claude model through OpenRouter.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "mock").lower()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Default model: Claude Sonnet 4.5 via OpenRouter. Swap to a cheaper model
# for eval runs (e.g. anthropic/claude-haiku-4.5) or a newer one
# (anthropic/claude-sonnet-4.6, anthropic/claude-sonnet-5) as needed --
# check https://openrouter.ai/models?q=claude for current slugs/pricing.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")

# Optional headers OpenRouter uses for its public leaderboard / rankings.
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "https://github.com")
OPENROUTER_APP_NAME = os.environ.get("OPENROUTER_APP_NAME", "support-resolution-agent")

# --- LLM-as-judge (eval only) ------------------------------------------------
# A separate, stronger model used ONLY to score/compare mock vs agent
# responses during evaluation -- never used to run the agent itself. Kept
# deliberately distinct from OPENROUTER_MODEL (the agent's model) so a
# larger model can grade a smaller/cheaper one without conflating the two.
OPENROUTER_JUDGE_MODEL = os.environ.get("OPENROUTER_JUDGE_MODEL", "openai/gpt-4.1")

# --- Data paths --------------------------------------------------------------
ORDERS_PATH = BASE_DIR / "data" / "orders.json"
KB_PATH = BASE_DIR / "data" / "kb.json"
TRACES_DIR = BASE_DIR / "traces"
APPROVALS_PATH = BASE_DIR / "traces" / "_approvals.json"

TRACES_DIR.mkdir(parents=True, exist_ok=True)

# --- Agent behavior ----------------------------------------------------------
MAX_TOOL_ITERATIONS = 6  # hard stop so a runaway tool loop can't hang the demo

# OpenRouter defaults max_tokens to the model's max output (often 32k-65k+)
# if you don't set it, which some providers/keys reject outright if you
# don't have enough credit headroom reserved for that ceiling. Cap it
# explicitly -- these are generous for what this agent actually needs
# (a JSON classification, or a short customer reply / tool call).
MAX_TOKENS_CLASSIFY = int(os.environ.get("MAX_TOKENS_CLASSIFY", "300"))
MAX_TOKENS_RESOLVE = int(os.environ.get("MAX_TOKENS_RESOLVE", "800"))