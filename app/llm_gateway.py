"""
LLM Gateway - the single place marking/spec-parsing/scheme-of-work code talks to an LLM.

Keeping this isolated means:
- swapping models/providers later doesn't touch marking logic
- every call goes through the same JSON-parsing safety net
- you can add retry/rate-limit/cost-logging in one place

PROVIDER RESOLUTION - in priority order:
1. provider_override (a plain string "anthropic"/"ollama"/"azure_openai"/"openai_compatible")
   given by the caller - existing behaviour, used by spec parsing's
   SPEC_PARSING_LLM_PROVIDER env var. Config for that type comes from env vars only.
2. The active AIProviderConfig row in the database (Admin: AI Provider settings) - lets a
   deployment configure Anthropic, Ollama, Azure OpenAI, or any other OpenAI-compatible
   endpoint from the UI, with its own API key/URL, without touching .env or restarting.
   Several can be saved and switched between with one click.
3. Falls back to the original env-var-only config (LLM_PROVIDER, ANTHROPIC_API_KEY,
   OLLAMA_HOST, etc.) if no database config exists yet - so a fresh install with nothing
   configured in the UI still works exactly as it always did.

Four provider types are supported:
  anthropic          - hosted Claude via the Anthropic API. Costs money per call. Best quality.
  ollama             - a model running locally via Ollama (https://ollama.com). Free, no
                       account. Good for testing the pipeline at zero cost - see the note
                       in the Admin: AI Provider page about marking-quality expectations.
  azure_openai       - Azure OpenAI Service, for institutions with an existing Microsoft
                       enterprise agreement that includes it (NOT the same as Microsoft 365
                       Copilot seats or GitHub Copilot - neither of those expose a callable
                       API for this). Needs an endpoint, API key, deployment name, and API version.
  openai_compatible  - any other server that speaks the OpenAI chat-completions API shape
                       (OpenAI itself, a self-hosted LocalAI/vLLM/LM Studio server, etc.).
                       Needs a base URL, API key, and model name.

Both/all paths return the same parsed dict shape, so nothing else in the app needs to know
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


def _env_fallback_config(provider_type: str) -> dict:
    """The original env-var-only config for a given provider type - used both for an
    explicit provider_override string and as the final fallback when no database config
    exists yet."""
    if provider_type == "anthropic":
        return {"provider_type": "anthropic", "api_key": os.getenv("ANTHROPIC_API_KEY"), "model_name": ANTHROPIC_MODEL}
    if provider_type == "ollama":
        return {"provider_type": "ollama", "base_url": OLLAMA_HOST, "model_name": OLLAMA_MODEL}
    raise RuntimeError(
        f"Unknown LLM provider '{provider_type}' from an env var override - use 'anthropic' or 'ollama' "
        f"(azure_openai/openai_compatible must be configured via Admin: AI Provider, they have no env-var form)."
    )


def _resolve_config(provider_override: str | None, for_spec_parsing: bool, for_draft_generation: bool = False) -> dict:
    if provider_override:
        return _env_fallback_config(provider_override)

    # Try the database-backed config next - wrapped defensively so a fresh install
    # (table doesn't exist yet, migrations not run) falls through to env vars instead of
    # crashing every single AI call.
    try:
        from .database import SessionLocal
        from .models import AIProviderConfig
        db = SessionLocal()
        try:
            query = db.query(AIProviderConfig)

            if for_draft_generation:
                row = query.filter(AIProviderConfig.is_active_for_draft_generation == True).first()  # noqa: E712
                if row is not None:
                    return _row_to_config(row)
                # Standing fix, confirmed as a real requirement: draft generation
                # (scheme-of-work/lesson content a teacher reviews before use) does NOT
                # fall back to whatever's active for marking - it defaults straight to
                # Ollama, so activating a paid provider for the high-stakes task never
                # silently starts charging for these lower-stakes drafts too. An admin
                # can still point this specific category at a paid provider explicitly
                # via "Use for draft generation" if they genuinely want that.
                return _env_fallback_config("ollama")

            row = None
            if for_spec_parsing:
                row = query.filter(AIProviderConfig.is_active_for_spec_parsing == True).first()  # noqa: E712
            if row is None:
                row = query.filter(AIProviderConfig.is_active == True).first()  # noqa: E712
            if row is not None:
                return _row_to_config(row)
        finally:
            db.close()
    except Exception:
        pass  # table missing, DB unreachable, etc. - fall through to env vars below

    if for_draft_generation:
        return _env_fallback_config("ollama")
    fallback_type = (SPEC_PARSING_LLM_PROVIDER if for_spec_parsing else None) or LLM_PROVIDER
    return _env_fallback_config(fallback_type)


def _row_to_config(row) -> dict:
    return {
        "provider_type": row.provider_type,
        "api_key": row.api_key,
        "base_url": row.base_url,
        "model_name": row.model_name,
        "api_version": row.api_version,
    }


def _parse_json_response(text: str, provider: str = "unknown", debug_info: str = "", stop_reason: str | None = None) -> dict:
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
            "to a hosted provider (Anthropic/Azure OpenAI), which handles larger "
            "multi-document prompts far more reliably"
        ) if provider == "ollama" else (
            "check for an API error in the server logs (docker compose logs app) around "
            "this time, e.g. a rate limit, an invalid API key, or a wrong endpoint/deployment name"
        )
        debug_suffix = f" [debug: {debug_info}]" if debug_info else ""
        raise ValueError(f"[Provider used: {provider}] The model returned a completely empty response - {hint}.{debug_suffix}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Standing fix: a JSON parse failure with stop_reason=max_tokens is NOT a malformed-
        # JSON bug in the model's output - it's the response getting cut off mid-string
        # because the caller's max_tokens budget was too small for what was actually being
        # generated (confirmed as the real cause of a genuine "unterminated string" error on
        # a lesson outline with rich generated practical/resource content). Distinguish this
        # case explicitly rather than leaving it looking like a generic JSON bug, since the
        # fix (raise max_tokens on the calling code) is completely different from a genuine
        # malformed-output bug.
        if stop_reason == "max_tokens":
            raise ValueError(
                f"[Provider used: {provider}] The response was cut off before it finished, because "
                f"it hit the max_tokens limit for this call - it is not malformed JSON, it's an "
                f"incomplete one. This usually means the requested content (e.g. a longer lesson, or "
                f"one with several practicals/printable resources) genuinely needs more output than "
                f"this call was budgeted for. If this keeps happening, the max_tokens value for this "
                f"specific call needs raising in the code, or the request needs to ask for less in one "
                f"go (e.g. a shorter duration, or generating practical resources separately). "
                f"[debug: {debug_info}]"
            )
        debug_suffix = f"\n[debug: {debug_info}]" if debug_info else ""
        raise ValueError(
            f"[Provider used: {provider}] LLM did not return valid JSON: {e}\nFirst 500 chars of raw response:\n{text[:500]}{debug_suffix}"
        )


def _complete_json_anthropic(system: str, prompt: str, max_tokens: int, api_key: str | None, model: str | None) -> dict:
    import anthropic
    global _anthropic_client
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No Anthropic API key configured. Add one in Admin: AI Provider, or set "
            "ANTHROPIC_API_KEY in .env if not using the database-backed settings."
        )
    # A fresh client per call when a non-default key is in play (Admin: AI Provider may
    # have a different key than .env) - the module-level cache is only safe for the env-var
    # path, where the key genuinely can't change without a restart anyway.
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model or ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIConnectionError as e:
        # Standing fix: this used to surface as a bare "Connection error." with no
        # indication of what to actually check - confirmed as a real problem when a
        # "normal user" hit this on a real campus deployment, on the college network,
        # with no further clue. This is a SERVER-side failure (the machine running
        # CourseCraft couldn't reach Anthropic's API), not something caused by which
        # network the person using the browser is on - that distinction matters, since
        # it rules out "try a different wifi" as a fix and points at the actual place to
        # look: this server's own outbound internet access.
        raise RuntimeError(
            "Could not reach the Anthropic API from this server (api.anthropic.com). This is "
            "almost always one of: (1) this server/container has no outbound internet access "
            "configured, (2) a firewall or network policy on THIS server's network is blocking "
            "that domain specifically - common on locked-down college/campus networks, where "
            "it needs allowlisting the same way any other approved cloud service would, or "
            "(3) a proxy is required but not configured. Ask IT to confirm this server can "
            "reach api.anthropic.com on port 443, or check server logs for the exact "
            "connection error. This is unrelated to which network the person USING the app is "
            "on - the call happens from this server, not their device."
        ) from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(
            f"Anthropic API returned an error (status {e.status_code}): {e.message}. "
            f"A 401 usually means an invalid/expired API key; a 429 means a rate limit or "
            f"quota was hit; check the Anthropic console for account-level issues."
        ) from e

    text = "".join(block.text for block in response.content if block.type == "text")
    debug_info = (
        f"stop_reason={response.stop_reason}, "
        f"content_block_types={[b.type for b in response.content]}, "
        f"usage={response.usage}"
    )
    print(f"[llm_gateway] Anthropic response: {debug_info}, text_length={len(text)}", flush=True)
    return _parse_json_response(text, provider="anthropic", debug_info=debug_info, stop_reason=response.stop_reason)


def _complete_json_ollama(system: str, prompt: str, host: str | None, model: str | None) -> dict:
    import requests
    host = host or OLLAMA_HOST
    model = model or OLLAMA_MODEL
    try:
        resp = requests.post(
            f"{host}/api/chat",
            json={
                "model": model,
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
            f"Could not reach Ollama at {host}. Is it running? "
            f"(`ollama serve`, and `ollama pull {model}` if you haven't already). "
            f"If TIGARSEC Assess is running in Docker, the host usually needs to be "
            f"http://host.docker.internal:11434 rather than localhost - see README."
        ) from e

    data = resp.json()
    text = data.get("message", {}).get("content", "")
    return _parse_json_response(text, provider="ollama")


def _complete_json_azure_openai(system: str, prompt: str, max_tokens: int, endpoint: str, api_key: str, deployment: str, api_version: str) -> dict:
    import requests
    if not (endpoint and api_key and deployment):
        raise RuntimeError(
            "Azure OpenAI is selected but not fully configured - endpoint, API key, and "
            "deployment name are all required. Check Admin: AI Provider."
        )
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
    try:
        resp = requests.post(
            url,
            params={"api-version": api_version or "2024-10-21"},
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Could not reach Azure OpenAI at {endpoint}. Check the endpoint URL in Admin: "
            f"AI Provider is correct (it should look like https://<resource>.openai.azure.com), "
            f"and that this server has outbound network access to it."
        ) from e

    if resp.status_code != 200:
        raise RuntimeError(
            f"Azure OpenAI returned an error (status {resp.status_code}): {resp.text[:500]}. "
            f"A 401/403 usually means the API key is wrong; a 404 usually means the deployment "
            f"name doesn't exist on this resource; check Admin: AI Provider."
        )

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    finish_reason = data.get("choices", [{}])[0].get("finish_reason")
    return _parse_json_response(
        text, provider="azure_openai", debug_info=f"finish_reason={finish_reason}",
        stop_reason="max_tokens" if finish_reason == "length" else None,
    )


def _complete_json_openai_compatible(system: str, prompt: str, max_tokens: int, base_url: str, api_key: str | None, model: str | None) -> dict:
    import requests
    if not base_url:
        raise RuntimeError("An OpenAI-compatible endpoint is selected but no base URL is configured. Check Admin: AI Provider.")
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.post(
            url,
            headers=headers,
            json={
                "model": model or "default",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=180,
        )
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Could not reach the OpenAI-compatible endpoint at {base_url}. Check the base "
            f"URL in Admin: AI Provider, and that this server can reach it."
        ) from e

    if resp.status_code != 200:
        raise RuntimeError(
            f"The OpenAI-compatible endpoint returned an error (status {resp.status_code}): "
            f"{resp.text[:500]}. Check the API key and model name in Admin: AI Provider - some "
            f"servers also don't support response_format=json_object, which may be the cause."
        )

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    finish_reason = data.get("choices", [{}])[0].get("finish_reason")
    return _parse_json_response(
        text, provider="openai_compatible", debug_info=f"finish_reason={finish_reason}",
        stop_reason="max_tokens" if finish_reason == "length" else None,
    )


def test_connection(config: dict) -> tuple[bool, str]:
    """
    A cheap, admin-triggered reachability + credential check for a saved provider config -
    built directly in response to a real problem: a connection failure was only ever
    discovered when a real teacher tried to generate something for real. This lets an
    admin (or IT, diagnosing a firewall question) check "will this actually work from
    this server" in one click, without burning a real generation attempt.
    Returns (success, message) - message is shown either way, since a SUCCESS also
    confirms useful things (which model responded, etc.), not just PASS/FAIL.
    """
    provider = config.get("provider_type")
    try:
        if provider == "ollama":
            import requests
            host = config.get("base_url") or OLLAMA_HOST
            model = config.get("model_name") or OLLAMA_MODEL
            resp = requests.get(f"{host}/api/tags", timeout=15)
            resp.raise_for_status()
            available_models = [m.get("name", "") for m in resp.json().get("models", [])]
            if model not in available_models and not any(model in m for m in available_models):
                return False, (
                    f"Reached Ollama at {host}, but the model '{model}' isn't pulled there yet. "
                    f"Available: {', '.join(available_models) or '(none)'}. Run `ollama pull {model}` on that machine."
                )
            return True, f"Reached Ollama at {host} - model '{model}' is available."

        elif provider == "anthropic":
            result = _complete_json_anthropic(
                "Reply with only this exact JSON, nothing else: {\"ok\": true}",
                "Confirm connectivity.", 20, config.get("api_key"), config.get("model_name"),
            )
            return True, f"Reached the Anthropic API and got a valid response back (model: {config.get('model_name') or ANTHROPIC_MODEL})."

        elif provider == "azure_openai":
            result = _complete_json_azure_openai(
                "Reply with only this exact JSON, nothing else: {\"ok\": true}",
                "Confirm connectivity.", 20, config.get("base_url"), config.get("api_key"),
                config.get("model_name"), config.get("api_version"),
            )
            return True, f"Reached Azure OpenAI (deployment: {config.get('model_name')}) and got a valid response back."

        elif provider == "openai_compatible":
            result = _complete_json_openai_compatible(
                "Reply with only this exact JSON, nothing else: {\"ok\": true}",
                "Confirm connectivity.", 20, config.get("base_url"), config.get("api_key"), config.get("model_name"),
            )
            return True, f"Reached {config.get('base_url')} and got a valid response back."

        return False, f"Unknown provider type '{provider}'."
    except Exception as e:
        return False, str(e)


def get_effective_provider(provider_override: str | None = None, for_spec_parsing: bool = False, for_draft_generation: bool = False) -> str:
    """
    Lets a caller find out (before or after a call) which provider will actually handle it -
    used so error messages can state definitively which provider was used instead of
    leaving it as something the user has to trust the code review on.
    """
    return _resolve_config(provider_override, for_spec_parsing, for_draft_generation)["provider_type"]


def complete_json(system: str, prompt: str, max_tokens: int = 4000, provider_override: str | None = None, for_spec_parsing: bool = False, for_draft_generation: bool = False) -> dict:
    """
    Calls the active LLM provider and parses a JSON object out of the response.
    The system prompt MUST instruct the model to return ONLY raw JSON, no markdown fences,
    no preamble - this function has a fallback for stripping fences but don't rely on it.

    provider_override, if given, uses that provider (env-var config only) for just this
    call regardless of the database-configured active provider.
    for_spec_parsing routes through the database's is_active_for_spec_parsing config (or
    SPEC_PARSING_LLM_PROVIDER as env fallback) if one is set, falling back to the everyday
    active provider otherwise - unchanged from before.
    for_draft_generation routes through is_active_for_draft_generation if set, but falls
    back straight to OLLAMA if not - NOT to the everyday active provider - so scheme-of-
    work/lesson draft generation stays free by default even once a paid provider is
    configured for marking. Pass this for any task a teacher reviews/edits before it's
    used for real (SOW weeks, lesson content) - never for marking itself.
    """
    config = _resolve_config(provider_override, for_spec_parsing, for_draft_generation)
    provider = config["provider_type"]

    if provider == "ollama":
        return _complete_json_ollama(system, prompt, config.get("base_url"), config.get("model_name"))
    elif provider == "anthropic":
        return _complete_json_anthropic(system, prompt, max_tokens, config.get("api_key"), config.get("model_name"))
    elif provider == "azure_openai":
        return _complete_json_azure_openai(
            system, prompt, max_tokens, config.get("base_url"), config.get("api_key"),
            config.get("model_name"), config.get("api_version"),
        )
    elif provider == "openai_compatible":
        return _complete_json_openai_compatible(
            system, prompt, max_tokens, config.get("base_url"), config.get("api_key"), config.get("model_name"),
        )
    else:
        raise RuntimeError(f"Unknown LLM provider '{provider}'.")
