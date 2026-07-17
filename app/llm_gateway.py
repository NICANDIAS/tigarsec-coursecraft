"""
LLM Gateway - the single place marking/spec-parsing/scheme-of-work code talks to an LLM.

Keeping this isolated means:
- swapping models/providers later doesn't touch marking logic
- every call goes through the same JSON-parsing safety net
- you can add retry/rate-limit/cost-logging in one place

Two providers are supported, switchable via LLM_PROVIDER in .env:

  LLM_PROVIDER=anthropic  (default) - hosted Claude via the Anthropic API. Costs money
                           per call after the initial free trial credit. Best marking quality.

  LLM_PROVIDER=ollama      - a model running locally via Ollama (https://ollama.com).
                           Completely free, no account, no API key, runs on your own machine.
                           Good for testing the pipeline end-to-end at zero cost. Marking
                           quality/confidence calibration from small local models will be
                           rougher than a hosted frontier model - treat this as "does the
                           plumbing work", not "is this ready to mark real grades."

SPEC_PARSING_LLM_PROVIDER (optional) overrides the provider used JUST for spec parsing's AI
fallback/discovery calls, independent of LLM_PROVIDER used for everyday marking. Parsing a
spec is a rare, one-off action per document (not per-student), so it's a good place to spend
a little on the hosted model even if you're keeping marking on free/local Ollama day-to-day -
this removes the CPU/memory bottleneck that makes local parsing slow or prone to stalling on
documents that need many AI calls. Leave unset to use LLM_PROVIDER for parsing too.

Both paths return the same parsed dict shape, so nothing else in the app needs to know
which provider is active.
"""
import os
import json

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
SPEC_PARSING_LLM_PROVIDER = os.getenv("SPEC_PARSING_LLM_PROVIDER", "").strip().lower() or None

# Check https://docs.claude.com for the current recommended model string -
# this is read from env so you can change it without touching code.
ANTHROPIC_MODEL = os.getenv("TIGARSEC_LLM_MODEL", "claude-sonnet-5")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

_anthropic_client = None


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM did not return valid JSON: {e}\nFirst 500 chars of raw response:\n{text[:500]}"
        )


def _complete_json_anthropic(system: str, prompt: str, max_tokens: int) -> dict:
    import anthropic
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set, and LLM_PROVIDER is 'anthropic'. "
                "Either add a key to .env, or set LLM_PROVIDER=ollama to run fully free/local instead."
            )
        _anthropic_client = anthropic.Anthropic(api_key=api_key)

    response = _anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_json_response(text)


def _complete_json_ollama(system: str, prompt: str) -> dict:
    import requests
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "format": "json",  # asks Ollama to constrain output to valid JSON
                "stream": False,
            },
            timeout=600,  # local models on modest hardware can be slow - be generous
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? "
            f"(`ollama serve`, and `ollama pull {OLLAMA_MODEL}` if you haven't already). "
            f"If TIGARSEC Assess is running in Docker, OLLAMA_HOST usually needs to be "
            f"http://host.docker.internal:11434 rather than localhost - see README."
        ) from e

    data = resp.json()
    text = data.get("message", {}).get("content", "")
    return _parse_json_response(text)


def get_effective_provider(provider_override: str | None = None) -> str:
    """
    Lets a caller find out (before or after a call) which provider will actually handle it -
    used so error messages can state definitively "this call used Ollama/Anthropic" instead
    of leaving it as something the user has to trust the code review on. Given a confusing
    string of failures that looked exactly like a small local model struggling, this makes
    that observable fact instead of a guess.
    """
    return provider_override or LLM_PROVIDER


def complete_json(system: str, prompt: str, max_tokens: int = 4000, provider_override: str | None = None) -> dict:
    """
    Calls the active LLM provider and parses a JSON object out of the response.
    The system prompt MUST instruct the model to return ONLY raw JSON, no markdown fences,
    no preamble - this function has a fallback for stripping fences but don't rely on it.

    provider_override, if given, uses that provider for just this call regardless of
    LLM_PROVIDER - used by spec parsing to optionally route through SPEC_PARSING_LLM_PROVIDER.
    """
    provider = (provider_override or LLM_PROVIDER)
    if provider == "ollama":
        return _complete_json_ollama(system, prompt)
    elif provider == "anthropic":
        return _complete_json_anthropic(system, prompt, max_tokens)
    else:
        raise RuntimeError(f"Unknown LLM provider '{provider}' - use 'anthropic' or 'ollama'.")
