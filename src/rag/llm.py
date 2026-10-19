"""Generate a grounded answer from retrieved chunks, as a LangChain chain.

    ANSWER_PROMPT | chat model | StrOutputParser

The chat model is chosen with LLM_PROVIDER:
- "ollama" (default): ChatOllama, a local model served by Ollama (http://127.0.0.1:11434).
  Free, no usage limits, and the retrieved document text never leaves this machine.
  Model: OLLAMA_MODEL (default gemma3:4b-it-q4_K_M, which fits a 6 GB GPU next to the
  embedding and reranker models). OLLAMA_URL overrides the server address. OLLAMA_KEEP_ALIVE
  (default 30m) is how long the model stays loaded after an answer; Ollama's own default of
  5 minutes made the first answer after a short break wait about a minute for a reload.
- "gemini": ChatGoogleGenerativeAI, Google Gemini (free tier; needs GEMINI_API_KEY).

Either way the model only ever receives the passages the caller was authorized to read
(retrieval applies the access filter in SQL first), and citations are checked afterwards.
"""

import json
import os
import urllib.error
import urllib.request

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama

from .retriever import SearchResult

# Listed as Free Tier in Google's Gemini API pricing docs. Do not switch to a
# paid-only model: this project runs without any paid subscription.
GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OLLAMA_MODEL = "gemma3:4b-it-q4_K_M"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"
OLLAMA_TIMEOUT_SECONDS = 180
OLLAMA_CONTEXT_TOKENS = 8192  # question + up to 10 passages + instructions fit comfortably
TEMPERATURE = 0.1

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

# Context and question are template variables, so braces inside documents are never
# interpreted as template syntax.
ANSWER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_INSTRUCTION),
    ("human", "CONTEXT:\n<<<\n{context}\n>>>\n\nQUESTION:\n{question}"),
])


class LLMError(RuntimeError):
    pass


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "ollama").strip().lower() or "ollama"


def model_name() -> str:
    if provider() == "gemini":
        return GEMINI_MODEL
    return os.environ.get("OLLAMA_MODEL", "").strip() or DEFAULT_OLLAMA_MODEL


# Kept for callers that only display the model name.
MODEL_NAME = GEMINI_MODEL


def create_client() -> BaseChatModel:
    """The configured provider's LangChain chat model: ChatOllama or ChatGoogleGenerativeAI."""
    chosen = provider()
    if chosen == "ollama":
        return ChatOllama(
            model=model_name(),
            base_url=(os.environ.get("OLLAMA_URL", "").strip() or DEFAULT_OLLAMA_URL).rstrip("/"),
            temperature=TEMPERATURE,
            num_ctx=OLLAMA_CONTEXT_TOKENS,
            keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "").strip() or DEFAULT_OLLAMA_KEEP_ALIVE,
            client_kwargs={"timeout": OLLAMA_TIMEOUT_SECONDS},
        )
    if chosen != "gemini":
        raise LLMError(f"Unknown LLM_PROVIDER {chosen!r}: use 'ollama' or 'gemini'.")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY is not set. Add it to the .env file in the project root "
            "or set it as an environment variable."
        )
    from langchain_google_genai import ChatGoogleGenerativeAI  # only needed for the Gemini provider

    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=api_key,
        temperature=TEMPERATURE,
        # Short factual lookups don't need the model's "thinking" step;
        # turning it off saves free-tier tokens and latency.
        thinking_budget=0,
        # No automatic retries: each retry would spend another request of the daily free quota.
        max_retries=0,
    )


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


def answer_chain(chat_model: BaseChatModel) -> Runnable:
    """prompt -> model -> plain text."""
    return ANSWER_PROMPT | chat_model | StrOutputParser()


def _as_llm_error(error: Exception, chat_model: BaseChatModel) -> Exception:
    """Map provider errors to the messages the API shows; Gemini API errors pass through unwrapped."""
    from google.genai import errors as genai_errors
    from ollama import ResponseError

    if isinstance(error, ResponseError):
        if error.status_code == 404:
            name = getattr(chat_model, "model", "")
            return LLMError(f"The local model {name!r} is not installed. Run: ollama pull {name}")
        return LLMError(f"The local AI model failed ({error.status_code}): {str(error.error)[:200]}")
    if isinstance(error, (ConnectionError, httpx.TransportError, TimeoutError)):
        return LLMError("The local AI model (Ollama) is not reachable. Start Ollama and try again, "
                        "or use sources only.")
    # LangChain wraps Gemini errors; the API maps the original (quota, overload) to clear responses.
    cause = error.__cause__
    if isinstance(cause, genai_errors.APIError):
        return cause
    return error


def generate_answer(chat_model: BaseChatModel, question: str, results: list[SearchResult]) -> str:
    try:
        text = answer_chain(chat_model).invoke({"context": build_context(results), "question": question})
    except Exception as error:
        mapped = _as_llm_error(error, chat_model)
        if mapped is error:
            raise
        raise mapped from error
    text = text.strip()
    if not text:
        raise LLMError("The AI model returned no text.")
    return text


def warm_up(chat_model) -> bool:
    """Load the local model into memory now, so the first question does not wait for it.

    Sends no prompt and generates nothing. Returns False (never raises) if Ollama is not
    running or the model is not an Ollama one; the first answer then simply loads the model.
    """
    if not isinstance(chat_model, ChatOllama):
        return False
    body = json.dumps({"model": chat_model.model, "keep_alive": chat_model.keep_alive}).encode()
    request = urllib.request.Request(f"{chat_model.base_url}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            response.read()
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False
