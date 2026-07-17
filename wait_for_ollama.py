"""
Runs once at container startup (only when LLM_PROVIDER=ollama) to make the whole stack
truly "docker compose up --build and it just works":
1. waits for the bundled Ollama service to come up
2. checks if the configured model is already downloaded
3. if not, pulls it (first run only - subsequent restarts skip this since the model
   persists in the ollama_data Docker volume)
"""
import os
import sys
import time
import json
import requests

HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")


def main():
    print(f"[startup] Checking Ollama at {HOST} ...", flush=True)

    reachable = False
    for _ in range(90):  # up to 3 minutes - the ollama container itself takes a moment to boot
        try:
            r = requests.get(f"{HOST}/api/tags", timeout=3)
            if r.ok:
                reachable = True
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)

    if not reachable:
        print(f"[startup] WARNING: Ollama not reachable at {HOST} after 3 minutes.", flush=True)
        print("[startup] Continuing to start the app anyway - marking calls will fail until Ollama is up.", flush=True)
        return

    existing = [m.get("name", "") for m in r.json().get("models", [])]
    model_base = MODEL.split(":")[0]
    if any(m == MODEL or m.startswith(model_base) for m in existing):
        print(f"[startup] Model '{MODEL}' already present - skipping download.", flush=True)
        return

    print(f"[startup] Pulling model '{MODEL}' - first run only, this can take several minutes "
          f"depending on your internet connection and the model size.", flush=True)
    try:
        with requests.post(f"{HOST}/api/pull", json={"name": MODEL}, stream=True, timeout=1800) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    status = json.loads(line).get("status", "")
                    if status:
                        print(f"[startup] {status}", flush=True)
                except (json.JSONDecodeError, AttributeError):
                    pass
        print(f"[startup] Model '{MODEL}' ready.", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"[startup] WARNING: automatic pull failed ({e}).", flush=True)
        print(f"[startup] You can pull it manually with: docker compose exec ollama ollama pull {MODEL}", flush=True)


if __name__ == "__main__":
    main()
