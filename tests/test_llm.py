"""Unit tests for the answer-model providers (no database, no real model).

The Ollama path is tested against a fake Ollama server on a random local port.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_llm -v
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from src.rag import llm
from src.rag.retriever import SearchResult


def result(text: str, source: str = "handbook.md") -> SearchResult:
    return SearchResult(chunk_id=f"{source}#0", source=source, relative_path=source, source_type="local", doc_id=None,
                        page_number=None, section="Leave", text=text, score=0.5)


class FakeOllama(BaseHTTPRequestHandler):
    reply: dict = {}
    status = 200
    received: list = []

    def do_POST(self):
        FakeOllama.received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        body = json.dumps(FakeOllama.reply).encode()
        self.send_response(FakeOllama.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProviderTests(unittest.TestCase):
    def test_ollama_is_the_default_provider(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_PROVIDER", None)
            os.environ.pop("OLLAMA_MODEL", None)
            client = llm.create_client()
            self.assertIsInstance(client, llm.OllamaClient)
            self.assertEqual(client.model, llm.DEFAULT_OLLAMA_MODEL)
            self.assertEqual(client.url, llm.DEFAULT_OLLAMA_URL)
            self.assertEqual(llm.model_name(), llm.DEFAULT_OLLAMA_MODEL)

    def test_settings_choose_model_and_server(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "Ollama", "OLLAMA_MODEL": "qwen2.5:7b",
                                          "OLLAMA_URL": "http://127.0.0.1:9999/"}):
            client = llm.create_client()
            self.assertEqual((client.model, client.url), ("qwen2.5:7b", "http://127.0.0.1:9999"))

    def test_keep_alive_defaults_to_30_minutes_and_is_configurable(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_KEEP_ALIVE": ""}):
            self.assertEqual(llm.create_client().keep_alive, "30m")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_KEEP_ALIVE": "2h"}):
            self.assertEqual(llm.create_client().keep_alive, "2h")

    def test_gemini_still_available_and_needs_a_key(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": ""}):
            self.assertEqual(llm.model_name(), llm.GEMINI_MODEL)
            with self.assertRaises(llm.LLMError):
                llm.create_client()

    def test_unknown_provider_is_rejected(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "something-else"}):
            with self.assertRaisesRegex(llm.LLMError, "Unknown LLM_PROVIDER"):
                llm.create_client()


class OllamaGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = llm.OllamaClient(url=f"http://127.0.0.1:{cls.server.server_port}", model="test-model")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOllama.status, FakeOllama.received = 200, []

    def test_sends_only_the_given_passages_and_returns_the_answer(self):
        FakeOllama.reply = {"message": {"role": "assistant", "content": "  Employees get 20 days [1].  "}, "done": True}
        answer = llm.generate_answer(self.client, "How many leave days?", [result("Employees receive 20 days of leave.")])
        self.assertEqual(answer, "Employees get 20 days [1].")
        path, body = FakeOllama.received[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0], {"role": "system", "content": llm.SYSTEM_INSTRUCTION})
        prompt = body["messages"][1]["content"]
        self.assertIn("[1] handbook.md | Section: Leave\nEmployees receive 20 days of leave.", prompt)
        self.assertIn("QUESTION:\nHow many leave days?", prompt)
        self.assertEqual(body["options"]["temperature"], 0.1)
        self.assertEqual(body["keep_alive"], "30m")

    def test_warm_up_loads_the_model_without_a_prompt(self):
        FakeOllama.reply = {"model": "test-model", "done": True}
        self.assertTrue(llm.warm_up(self.client))
        path, body = FakeOllama.received[0]
        self.assertEqual(path, "/api/generate")
        self.assertEqual(body, {"model": "test-model", "keep_alive": "30m"})

    def test_warm_up_never_raises(self):
        self.assertFalse(llm.warm_up(llm.OllamaClient(url="http://127.0.0.1:9", model="m")))  # Ollama not running
        self.assertFalse(llm.warm_up(object()))  # Gemini client: nothing to load
        self.assertFalse(llm.warm_up(None))  # no client configured

    def test_missing_model_gives_a_clear_error(self):
        FakeOllama.status, FakeOllama.reply = 404, {"error": "model 'test-model' not found"}
        with self.assertRaisesRegex(llm.LLMError, "not installed. Run: ollama pull test-model"):
            llm.generate_answer(self.client, "q", [result("t")])

    def test_empty_answer_is_an_error(self):
        FakeOllama.reply = {"message": {"content": ""}, "done_reason": "length"}
        with self.assertRaisesRegex(llm.LLMError, "returned no text"):
            llm.generate_answer(self.client, "q", [result("t")])

    def test_unreachable_server_is_a_clear_error(self):
        closed = llm.OllamaClient(url="http://127.0.0.1:9", model="m")  # nothing listens on port 9
        with self.assertRaisesRegex(llm.LLMError, "not reachable"):
            llm.generate_answer(closed, "q", [result("t")])


if __name__ == "__main__":
    unittest.main()
