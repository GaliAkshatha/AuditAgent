"""
AuditAgent v4 — LLM Provider (shared, multi-turn)
A small shared abstraction over Anthropic/Gemini for multi-turn chat,
used by chatbot.py. architect.py has its own single-turn version of this
(call_architect_model) since it only ever sends one prompt — this one
carries a message history back and forth, which the chatbot needs.
"""

import os
import time


def chat_completion(messages: list[dict], provider: str = "anthropic", model: str | None = None) -> str:
    """messages: list of {"role": "user"|"assistant", "content": str}, oldest first."""
    if provider == "gemini":
        return _gemini_chat(messages, model or "gemini-2.5-flash")
    return _anthropic_chat(messages, model or "claude-sonnet-4-6")


def _anthropic_chat(messages: list[dict], model: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("The 'anthropic' package isn't installed. Run: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. Get a key from console.anthropic.com."
        )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(model=model, max_tokens=1200, messages=messages)
    return "".join(block.text for block in response.content if hasattr(block, "text"))


def _gemini_chat(messages: list[dict], model: str, max_retries: int = 3) -> str:
    try:
        from google import genai
        from google.genai import errors as genai_errors
    except ImportError:
        raise RuntimeError("The 'google-genai' package isn't installed. Run: pip install google-genai")

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable not set. Get a free key from "
            "Google AI Studio (aistudio.google.com/apikey)."
        )

    client = genai.Client(api_key=api_key)

    # Gemini's chat format differs from Anthropic's — convert {"role": "user"/"assistant", "content": ...}
    # into Gemini's {"role": "user"/"model", "parts": [...]}.
    contents = [
        {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
        for m in messages
    ]

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(model=model, contents=contents)
            return response.text
        except genai_errors.ServerError as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except genai_errors.ClientError as e:
            raise RuntimeError(f"Gemini API error (not retryable): {e}")

    raise RuntimeError(f"Gemini's API stayed unavailable after {max_retries} attempts ({last_error}).")
