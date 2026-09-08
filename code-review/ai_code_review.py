"""
ai_code_review.py - independent review of live-execution code, by a model that did not write it.

Where this fits in the system
------------------------------
The execution code was generated and iterated on FAST with an LLM. Fast LLM codegen is
productive but introduces a specific, dangerous bug class in a live-trading script:
unconfirmed order fills, positions left open, wrong leverage/volume math, and missing
error handling on broker calls. Those are exactly the failures that need catching before
they cost anything.

So Nemotron is used here as an INDEPENDENT reviewer of the *code* (never to place or gate
trades - trading happens only through the deterministic execution scripts and the author's
own intervention). Two jobs:
  1. review_code(path)      - read an execution script and flag risky/buggy patterns.
  2. flag_anomalies(status) - periodic health check on the live state so the author can
                              see at a glance whether anything looks wrong.

NVIDIA NIM hosts Nemotron and exposes an OpenAI-WIRE-COMPATIBLE endpoint, so we use the
`openai` client pointed at NVIDIA's base_url. There is no OpenAI account or server
involved - this is purely a call to NVIDIA's hosted model. No local GPU needed.

Setup (one-off):
    1. Sign in at https://build.nvidia.com (Google/email, no card).
    2. Open a Nemotron model, click "Get API Key" -> copy the `nvapi-...` key.
    3. setx NVIDIA_API_KEY "nvapi-..."   (then reopen the terminal)

Run a real review of the live executor:
    python ai_code_review.py ../execution/mt5_execute_finals.py

If NVIDIA_API_KEY is not set this prints setup steps and exits - it never fabricates a
review.
"""

import os
import sys
import json

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Current-gen NVIDIA Nemotron. Override with NVIDIA_NIM_MODEL if your account exposes another.
DEFAULT_MODEL = os.environ.get("NVIDIA_NIM_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")


class IndependentCodeReviewer:
    """Independent Nemotron reviewer for fast-generated execution code + live state."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY not set. Get a free key at https://build.nvidia.com "
                "('Get API Key' on any Nemotron model), then:\n"
                '    setx NVIDIA_API_KEY "nvapi-..."   (reopen the terminal afterwards)'
            )
        # openai client pointed at NVIDIA's endpoint - NVIDIA only, no OpenAI involved.
        from openai import OpenAI
        self.client = OpenAI(api_key=self.api_key, base_url=NIM_BASE_URL)
        self.model = model

    def _chat(self, system: str, user: str, max_tokens: int = 2048) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        msg = resp.choices[0].message
        # Nemotron reasoning models may return the answer in reasoning_content if content is empty.
        return msg.content or getattr(msg, "reasoning_content", None) or "(model returned no content)"

    def review_code(self, path: str) -> str:
        """Send an execution script to Nemotron and ask for a prioritised risk review."""
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        system = (
            "You are an independent code reviewer for LIVE algorithmic-trading execution "
            "scripts. The author iterates on this code rapidly with an LLM, so your job is to "
            "catch bugs that could cause real money loss before they run. Prioritise this bug "
            "class specifically:\n"
            "  - orders sent but the fill never confirmed (assuming success)\n"
            "  - positions opened but left open / not closed on exit or at the cutoff\n"
            "  - wrong leverage or volume/lot math\n"
            "  - missing error handling around broker calls (initialize, order_send, ticks)\n"
            "  - race conditions or off-by-one in the stack/poll/exit loop\n"
            "Return a numbered list, highest risk first. For each: the concern, where in the "
            "code, and a one-line fix. Be concrete; cite function names. If something is fine, "
            "say so briefly rather than inventing problems."
        )
        user = f"Review this execution script ({os.path.basename(path)}):\n\n```python\n{source}\n```"
        return self._chat(system, user, max_tokens=3000)

    def flag_anomalies(self, status: dict) -> str:
        """Periodic health check: given a live-state snapshot, flag anything that looks off."""
        system = (
            "You are a monitoring assistant for a live-trading run. You do NOT trade. Given a "
            "state snapshot, tell the operator in 2-4 bullet points whether anything looks "
            "wrong - e.g. open positions that should be flat, orders with no confirmed fill, "
            "leverage outside the intended range, or a stale/!=expected value. Start with "
            "'OK' or 'CHECK'. Be terse and specific."
        )
        user = "Live state snapshot:\n" + json.dumps(status, indent=2)
        return self._chat(system, user, max_tokens=400)


if __name__ == "__main__":
    try:
        reviewer = IndependentCodeReviewer()
    except RuntimeError as e:
        print(e)
        sys.exit(1)

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "execution", "mt5_execute_finals.py"
    )
    print(f"# Nemotron code review of {os.path.basename(target)}\n")
    print(reviewer.review_code(target))
