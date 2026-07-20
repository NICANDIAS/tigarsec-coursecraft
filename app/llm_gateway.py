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


def _parse_json_response(text: str, provider: str = "unknown", debug_info: str = "") -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    if not text:
        # An entirely empty response is a distinct, more diagnosable failure than
        # malformed JSON - most commonly the local model running out of usable context
        # window on a large combined prompt (several files + a long custom rubric), or a
        # dropped/errored connection that didn't raise a proper exception upstream.
        hint = (
            "the combined prompt (files + rubric + context) may be too large for the "
            "local Ollama model's context window - try shortening the custom marking "
            "scheme text, marking fewer files at once, or switching this course/submission "
            "to LLM_PROVIDER=anthropic, which handles larger multi-document prompts far "
            "more reliably"
        ) if provider == "ollama" else (
            "this is unusual for Anthropic specifically - check for an API error in the "
            "server logs (docker compose logs app) around this time, e.g. a rate limit or "
            "an invalid API key"
        )
        debug_suffix = f" [debug: {debug_info}]" if debug_info else ""
        raise ValueError(f"[Provider used: {provider}] The model returned a completely empty response - {hint}.{debug_suffix}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"[Provider used: {provider}] LLM did not return valid JSON: {e}\nFirst 500 chars of raw response:\n{text[:500]}"
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
    debug_info = (
        f"stop_reason={response.stop_reason}, "
        f"content_block_types={[b.type for b in response.content]}, "
        f"usage={response.usage}"
    )
    # Printed directly to stdout, immediately, unconditionally - visible via
    # `docker compose logs app` regardless of what happens to the exception object
    # afterward (retry loops, exception re-wrapping, etc.) - the most reliable place to
    # see this, since the error-message route has repeatedly not shown it for reasons
    # not yet understood.
    print(f"[llm_gateway] Anthropic response: {debug_info}, text_length={len(text)}", flush=True)
    return _parse_json_response(text, provider="anthropic", debug_info=debug_info)


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
    return _parse_json_response(text, provider="ollama")


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
