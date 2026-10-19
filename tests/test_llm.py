"""Unit tests for the LangChain answer chain and its providers (no database, no real model).

The Ollama path runs ChatOllama against a fake Ollama server on a random local port.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_llm -v
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from google.genai import errors as genai_errors
from langchain_core.runnables import RunnableLambda
from langchain_ollama import ChatOllama

from src.rag import llm
from src.rag.retriever import SearchResult


def result(text: str, source: str = "handbook.md") -> SearchResult:
    return SearchResult(chunk_id=f"{source}#0", source=source, relative_path=source, source_type="local", doc_id=None,
                        page_number=None, section="Leave", text=text, score=0.5)


class FakeOllama(BaseHTTPRequestHandler):
    """Answers /api/chat like Ollama (streamed or not) with `content`, or `status` + `error`."""

    content = ""
    status = 200
    error = ""
    received: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.received.append((self.path, body))
        if FakeOllama.status != 200:
            payload, content_type = json.dumps({"error": FakeOllama.error}).encode(), "application/json"
        else:
            base = {"model": body.get("model"), "created_at": "2026-01-01T00:00:00Z"}
            message = {"role": "assistant", "content": FakeOllama.content}
            if body.get("stream", True):
                lines = [{**base, "message": message, "done": False},
                         {**base, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"}]
                payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
                content_type = "application/x-ndjson"
            else:
                payload = json.dumps({**base, "message": message, "done": True, "done_reason": "stop"}).encode()
                content_type = "application/json"
        self.send_response(FakeOllama.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class ProviderTests(unittest.TestCase):
    def test_ollama_is_the_default_provider(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("LLM_PROVIDER", "OLLAMA_MODEL", "OLLAMA_URL", "OLLAMA_KEEP_ALIVE"):
                os.environ.pop(name, None)
            model = llm.create_client()
            self.assertIsInstance(model, ChatOllama)
            self.assertEqual((model.model, model.base_url), (llm.DEFAULT_OLLAMA_MODEL, llm.DEFAULT_OLLAMA_URL))
            self.assertEqual((model.temperature, model.num_ctx), (0.1, llm.OLLAMA_CONTEXT_TOKENS))
            self.assertEqual(llm.model_name(), llm.DEFAULT_OLLAMA_MODEL)

    def test_settings_choose_model_and_server(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "Ollama", "OLLAMA_MODEL": "qwen2.5:7b",
                                          "OLLAMA_URL": "http://127.0.0.1:9999/"}):
            model = llm.create_client()
            self.assertEqual((model.model, model.base_url), ("qwen2.5:7b", "http://127.0.0.1:9999"))

    def test_keep_alive_defaults_to_30_minutes_and_is_configurable(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_KEEP_ALIVE": ""}):
            self.assertEqual(llm.create_client().keep_alive, "30m")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_KEEP_ALIVE": "2h"}):
            self.assertEqual(llm.create_client().keep_alive, "2h")

    def test_gemini_needs_a_key_and_never_retries(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": ""}):
            self.assertEqual(llm.model_name(), llm.GEMINI_MODEL)
            with self.assertRaises(llm.LLMError):
                llm.create_client()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key-not-real"}):
            model = llm.create_client()
            self.assertEqual((model.model, model.temperature, model.thinking_budget, model.max_retries),
                             (llm.GEMINI_MODEL, 0.1, 0, 0))

    def test_unknown_provider_is_rejected(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "something-else"}):
            with self.assertRaisesRegex(llm.LLMError, "Unknown LLM_PROVIDER"):
                llm.create_client()


class PromptTests(unittest.TestCase):
    def test_prompt_is_the_system_rules_plus_numbered_context(self):
        system, human = llm.ANSWER_PROMPT.format_messages(
            context=llm.build_context([result("Employees receive 20 days of leave.")]), question="How many leave days?")
        self.assertEqual((system.type, system.content), ("system", llm.SYSTEM_INSTRUCTION))
        self.assertEqual(human.content, "CONTEXT:\n<<<\n[1] handbook.md | Section: Leave\nEmployees receive 20 days "
                                        "of leave.\n>>>\n\nQUESTION:\nHow many leave days?")

    def test_braces_in_documents_are_not_template_syntax(self):
        _, human = llm.ANSWER_PROMPT.format_messages(context="config: {question} {{x}}", question="q")
        self.assertIn("config: {question} {{x}}", human.content)


class OllamaGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "test-model",
                                          "OLLAMA_URL": f"http://127.0.0.1:{cls.server.server_port}"}):
            cls.model = llm.create_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOllama.status, FakeOllama.error, FakeOllama.received = 200, "", []

    def test_sends_only_the_given_passages_and_returns_the_answer(self):
        FakeOllama.content = "  Employees get 20 days [1].  "
        answer = llm.generate_answer(self.model, "How many leave days?", [result("Employees receive 20 days of leave.")])
        self.assertEqual(answer, "Employees get 20 days [1].")
        path, body = FakeOllama.received[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"][0], {"role": "system", "content": llm.SYSTEM_INSTRUCTION})
        self.assertEqual(body["messages"][1]["role"], "user")
        prompt = body["messages"][1]["content"]
        self.assertIn("[1] handbook.md | Section: Leave\nEmployees receive 20 days of leave.", prompt)
        self.assertIn("QUESTION:\nHow many leave days?", prompt)
        self.assertEqual((body["options"]["temperature"], body["options"]["num_ctx"]), (0.1, llm.OLLAMA_CONTEXT_TOKENS))
        self.assertEqual(body["keep_alive"], "30m")

    def test_missing_model_gives_a_clear_error(self):
        FakeOllama.status, FakeOllama.error = 404, "model 'test-model' not found"
        with self.assertRaisesRegex(llm.LLMError, "not installed. Run: ollama pull test-model"):
            llm.generate_answer(self.model, "q", [result("t")])

    def test_other_server_errors_are_reported(self):
        FakeOllama.status, FakeOllama.error = 500, "out of memory"
        with self.assertRaisesRegex(llm.LLMError, r"failed \(500\): out of memory"):
            llm.generate_answer(self.model, "q", [result("t")])

    def test_empty_answer_is_an_error(self):
        FakeOllama.content = ""
        with self.assertRaisesRegex(llm.LLMError, "returned no text"):
            llm.generate_answer(self.model, "q", [result("t")])

    def test_unreachable_server_is_a_clear_error(self):
        closed = ChatOllama(model="m", base_url="http://127.0.0.1:9")  # nothing listens on port 9
        with self.assertRaisesRegex(llm.LLMError, "not reachable"):
            llm.generate_answer(closed, "q", [result("t")])

    def test_warm_up_loads_the_model_without_a_prompt(self):
        FakeOllama.content = ""
        self.assertTrue(llm.warm_up(self.model))
        path, body = FakeOllama.received[0]
        self.assertEqual(path, "/api/generate")
        self.assertEqual(body, {"model": "test-model", "keep_alive": "30m"})

    def test_warm_up_never_raises(self):
        self.assertFalse(llm.warm_up(ChatOllama(model="m", base_url="http://127.0.0.1:9")))  # Ollama not running
        self.assertFalse(llm.warm_up(object()))  # not an Ollama model: nothing to load
        self.assertFalse(llm.warm_up(None))  # no model configured


class GeminiErrorTests(unittest.TestCase):
    def test_wrapped_gemini_api_errors_reach_the_api_unwrapped(self):
        quota = genai_errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded. Please retry in 30s.",
                                                         "status": "RESOURCE_EXHAUSTED"}})

        def fail(_):
            try:
                raise quota
            except genai_errors.ClientError as error:
                raise RuntimeError("wrapped by LangChain") from error

        with self.assertRaises(genai_errors.ClientError) as caught:
            llm.generate_answer(RunnableLambda(fail), "q", [result("t")])
        self.assertIs(caught.exception, quota)


if __name__ == "__main__":
    unittest.main()
