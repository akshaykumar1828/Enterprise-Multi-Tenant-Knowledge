"""Generate a grounded answer from retrieved chunks.

Two providers, chosen with LLM_PROVIDER:
- "ollama" (default): a local model served by Ollama (http://127.0.0.1:11434). Free, no
  usage limits, and the retrieved document text never leaves this machine.
  Model: OLLAMA_MODEL (default gemma3:4b-it-q4_K_M, which fits a 6 GB GPU next to the
  embedding and reranker models). OLLAMA_URL overrides the server address. OLLAMA_KEEP_ALIVE
  (default 30m) is how long the model stays loaded after an answer; Ollama's own default of
  5 minutes made the first answer after a short break wait about a minute for a reload.
- "gemini": Google Gemini (free tier; needs GEMINI_API_KEY).

Either way the model only ever receives the passages the caller was authorized to read
(retrieval applies the access filter in SQL first), and citations are checked afterwards.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from .retriever import SearchResult

# Listed as Free Tier in Google's Gemini API pricing docs. Do not switch to a
# paid-only model: this project runs without any paid subscription.
GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OLLAMA_MODEL = "gemma3:4b-it-q4_K_M"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"
OLLAMA_TIMEOUT_SECONDS = 180
OLLAMA_CONTEXT_TOKENS = 8192  # question + up to 10 passages + instructions fit comfortably

SYSTEM_INSTRUCTION = """You answer questions about company documents.

Rules:
- Answer using ONLY the information in the CONTEXT section.
- Do not invent facts, numbers, names, or policies.
- Do not use outside knowledge to fill gaps, even if it seems reasonable.
- If the context does not contain the answer, say clearly that the information is not available in the provided documents. Do not guess.
- If the context answers only part of the question, answer that part and say what is missing.
- Answer clearly and concisely.

Citations:
- The context is split into numbered sources: [1], [2], [3], ...
- After every statement that uses the context, cite the supporting source number(s) in square brackets, for example [1] or [1][3].
- Only use source numbers that appear in the CONTEXT. Never cite a number that is not there.
- Cite by number only. Do not write file names, document ids, page numbers, or other source details yourself.
- Never attach a citation to a statement that the context does not support.
- If the information is not available in the provided documents, say so and do not add any citation."""


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaClient:
    url: str
    model: str
    keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "ollama").strip().lower() or "ollama"


def model_name() -> str:
    if provider() == "gemini":
        return GEMINI_MODEL
    return os.environ.get("OLLAMA_MODEL", "").strip() or DEFAULT_OLLAMA_MODEL


# Kept for callers that only display the model name.
MODEL_NAME = GEMINI_MODEL


def create_client():
    """The configured provider's client: an OllamaClient, or a google-genai Client."""
    chosen = provider()
    if chosen == "ollama":
        return OllamaClient(url=(os.environ.get("OLLAMA_URL", "").strip() or DEFAULT_OLLAMA_URL).rstrip("/"),
                            model=model_name(),
                            keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "").strip() or DEFAULT_OLLAMA_KEEP_ALIVE)
    if chosen != "gemini":
        raise LLMError(f"Unknown LLM_PROVIDER {chosen!r}: use 'ollama' or 'gemini'.")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY is not set. Add it to the .env file in the project root "
            "or set it as an environment variable."
        )
    from google import genai  # only needed for the Gemini provider

    return genai.Client(api_key=api_key)


def build_context(results: list[SearchResult]) -> str:
    blocks = []
    for number, result in enumerate(results, start=1):
        # The label is exactly the citation the model should write: [1], [2], ...
        header = f"[{number}] {result.source}"
        if result.page_number is not None:
            header += f" | Page: {result.page_number}"
        header += f" | Section: {result.section or 'n/a'}"
        blocks.append(f"{header}\n{result.text}")
    return "\n\n---\n\n".join(blocks)


def build_prompt(question: str, context: str) -> str:
    return f"CONTEXT:\n<<<\n{context}\n>>>\n\nQUESTION:\n{question}"


def _generate_ollama(client: OllamaClient, prompt: str) -> str:
    body = json.dumps({
        "model": client.model,
        "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": client.keep_alive,
        "options": {"temperature": 0.1, "num_ctx": OLLAMA_CONTEXT_TOKENS},
    }).encode()
    request = urllib.request.Request(f"{client.url}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        if error.code == 404:
            raise LLMError(f"The local model {client.model!r} is not installed. Run: ollama pull {client.model}") from None
        raise LLMError(f"The local AI model failed ({error.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise LLMError("The local AI model (Ollama) is not reachable. Start Ollama and try again, "
                       "or use sources only.") from error
    text = (data.get("message") or {}).get("content", "").strip()
    if not text:
        raise LLMError(f"The local AI model returned no text (done reason: {data.get('done_reason', 'unknown')})")
    return text


def warm_up(client) -> bool:
    """Load the local model into memory now, so the first question does not wait for it.

    Sends no prompt and generates nothing. Returns False (never raises) if Ollama is not
    running or the client is Gemini; the first answer then simply loads the model itself.
    """
    if not isinstance(client, OllamaClient):
        return False
    body = json.dumps({"model": client.model, "keep_alive": client.keep_alive}).encode()
    request = urllib.request.Request(f"{client.url}/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            response.read()
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _generate_gemini(client, prompt: str) -> str:
    from google.genai import types

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.1,
            # Short factual lookups don't need the model's "thinking" step;
            # turning it off saves free-tier tokens and latency.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    if not response.text:
        reason = response.candidates[0].finish_reason if response.candidates else "unknown"
        raise LLMError(f"Gemini returned no text (finish reason: {reason})")
    return response.text.strip()


def generate_answer(client, question: str, results: list[SearchResult]) -> str:
    prompt = build_prompt(question, build_context(results))
    if isinstance(client, OllamaClient):
        return _generate_ollama(client, prompt)
    return _generate_gemini(client, prompt)
