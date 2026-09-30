"""Generate a grounded answer from retrieved chunks with Google Gemini."""

import os

from google import genai
from google.genai import types

from .retriever import SearchResult

# Listed as Free Tier in Google's Gemini API pricing docs. Do not switch to a
# paid-only model: this project runs without any paid subscription.
MODEL_NAME = "gemini-2.5-flash"

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


def create_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY is not set. Add it to the .env file in the project root "
            "or set it as an environment variable."
        )
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


def generate_answer(client: genai.Client, question: str, results: list[SearchResult]) -> str:
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=build_prompt(question, build_context(results)),
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
