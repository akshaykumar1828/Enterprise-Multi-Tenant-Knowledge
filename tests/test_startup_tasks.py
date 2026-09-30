"""deploy\\start_caddy_task.ps1: waits for the API's health check, then launches Caddy.

The real wrapper runs against a fake health endpoint (a local HTTP server on a free
loopback port) and a fake "caddy" (a .cmd file that records how it was started), so
no real API, Caddy or scheduled task is involved. The env file mirrors a production
deploy\\caddy.env written by a Windows editor: UTF-8 byte-order mark, a frontend path
with spaces.

Regression: the production API answers {"status": "degraded"} when no Gemini key is
configured. The wrapper used to wait only for "ok", so the "RAG Caddy" task polled
until it gave up (and was restarted), never started Caddy, and logged nothing while
waiting.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_startup_tasks -v
"""

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PROJECT_ROOT / "deploy" / "start_caddy_task.ps1"

FAKE_CADDY_RECORD = r"""@echo off
> "%~dp0caddy-called.txt" echo ARGS=%*
>> "%~dp0caddy-called.txt" echo CWD=%CD%
>> "%~dp0caddy-called.txt" echo SITE_ADDRESS=%SITE_ADDRESS%
>> "%~dp0caddy-called.txt" echo FRONTEND_DIST=%FRONTEND_DIST%
>> "%~dp0caddy-called.txt" echo CADDY_ADMIN=%CADDY_ADMIN%
>> "%~dp0caddy-called.txt" echo CADDY_LOG_DIR=%CADDY_LOG_DIR%
"""
FAKE_CADDY = FAKE_CADDY_RECORD + "exit /b 0\n"                                   # exits at once
FAKE_CADDY_RUNNING = FAKE_CADDY_RECORD + "ping -n 7 127.0.0.1 >nul\nexit /b 0\n"   # stays up ~6 s, like a server
FAKE_CADDY_PORT_IN_USE = FAKE_CADDY_RECORD + (                                    # dies at startup, like Caddy does
    "echo Error: loading initial config: listen tcp :8080: bind: Only one usage of each socket address "
    "is normally permitted. 1>&2\nexit /b 1\n")
SYSTEM32 = Path(r"C:\Windows\System32")


class FakeHealth:
    """A loopback HTTP server answering /api/v1/health with a scripted sequence of (status, body)."""

    def __init__(self, *answers: tuple[int, dict]):
        self.answers = list(answers)
        self.paths: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.paths.append(self.path)
                code, body = owner.answers.pop(0) if len(owner.answers) > 1 else owner.answers[0]
                if self.path != "/api/v1/health":
                    code, body = 404, {"detail": "Not Found"}
                payload = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/health"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class CaddyStartupTaskTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rag-caddy-task-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logs = self.dir / "logs"
        self.dist = self.dir / "front end" / "dist"  # a path with spaces, like the real project folder
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
        self.caddy = self.dir / "caddy.cmd"
        self.caddy.write_text(FAKE_CADDY, encoding="ascii")
        self.env_file = self.dir / "caddy.env"
        # Same keys and order as the production file; written with a UTF-8 BOM (utf-8-sig).
        self.env_file.write_text("SITE_ADDRESS=http://localhost:8080\nCADDY_ADMIN=localhost:2999\n"
                                 f"FRONTEND_DIST={self.dist}\nCADDY_LOG_DIR={self.dir / 'caddy-logs'}\n",
                                 encoding="utf-8-sig")

    def run_wrapper(self, health_url: str | None, wait_seconds: int = 30, cwd: Path = PROJECT_ROOT,
                    caddy: Path | None = None) -> subprocess.CompletedProcess:
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER),
                "-CaddyExe", str(caddy or self.caddy), "-EnvFile", str(self.env_file), "-LogDir", str(self.logs),
                "-WaitSeconds", str(wait_seconds)]
        if health_url:
            args += ["-HealthUrl", health_url]
        return subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=wait_seconds + 60)

    def task_log(self) -> str:
        logs = sorted(self.logs.glob("caddy-*.task.log"))
        self.assertEqual(len(logs), 1, "exactly one task log per run")
        return logs[0].read_text(encoding="utf-8")

    def caddy_call(self) -> dict[str, str] | None:
        called = self.dir / "caddy-called.txt"
        if not called.exists():
            return None
        return dict(line.split("=", 1) for line in called.read_text().splitlines() if "=" in line)

    def assert_caddy_started_with_the_env_file(self):
        call = self.caddy_call()
        self.assertIsNotNone(call, "Caddy was not launched")
        self.assertIn("run --config", call["ARGS"])
        self.assertIn(str(PROJECT_ROOT / "deploy" / "Caddyfile"), call["ARGS"])
        self.assertIn("--adapter caddyfile", call["ARGS"])
        self.assertEqual(call["SITE_ADDRESS"].strip(), "http://localhost:8080")  # first line, after the BOM
        self.assertEqual(call["FRONTEND_DIST"].strip(), str(self.dist))
        self.assertEqual(call["CADDY_ADMIN"].strip(), "localhost:2999")

    # --- the regression -------------------------------------------------------------------------
    def test_degraded_api_is_ready_and_caddy_is_launched(self):
        with FakeHealth((200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_caddy_started_with_the_env_file()
        log = self.task_log()
        self.assertIn(f"waiting for the API at {health.url}", log)
        self.assertIn("API ready (HTTP 200 status=degraded); starting Caddy for http://localhost:8080", log)
        self.assertIn("Caddy exited with code 0", log)
        self.assertEqual(set(health.paths), {"/api/v1/health"})

    def test_ok_api_is_ready_too(self):
        with FakeHealth((200, {"status": "ok"})) as health:
            result = self.run_wrapper(health.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_caddy_started_with_the_env_file()
        self.assertIn("API ready (HTTP 200 status=ok)", self.task_log())

    def test_waits_while_starting_then_launches(self):
        with FakeHealth((503, {"status": "unavailable"}), (200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_caddy_started_with_the_env_file()
        log = self.task_log()
        self.assertLess(log.index("API not ready yet: HTTP 503"), log.index("API ready (HTTP 200 status=degraded)"))

    # --- no endless, silent waits -----------------------------------------------------------------
    def test_unavailable_api_gives_up_with_a_logged_reason(self):
        with FakeHealth((503, {"status": "unavailable"})) as health:
            result = self.run_wrapper(health.url, wait_seconds=2)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.caddy_call())
        log = self.task_log()
        self.assertIn("API not ready yet: HTTP 503", log)
        self.assertIn("after 2 s (last: HTTP 503); giving up", log)

    def test_no_api_at_all_gives_up_with_a_logged_reason(self):
        with FakeHealth((200, {"status": "ok"})) as health:
            closed_url = health.url
        result = self.run_wrapper(closed_url, wait_seconds=2)  # server already shut down
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.caddy_call())
        self.assertRegex(self.task_log(), r"API not ready yet: no answer \(.+\)")
        self.assertIn("giving up", self.task_log())

    def test_wrong_health_path_is_not_mistaken_for_ready(self):
        with FakeHealth((200, {"status": "ok"})) as health:
            result = self.run_wrapper(health.url.replace("/api/v1/health", "/health"), wait_seconds=2)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.caddy_call())
        self.assertIn("HTTP 404", self.task_log())

    def test_default_health_url_is_the_api_endpoint(self):
        default = re.search(r'\[string\]\s*\$HealthUrl\s*=\s*"([^"]+)"', WRAPPER.read_text(encoding="utf-8")).group(1)
        self.assertEqual(default, "http://127.0.0.1:8000/api/v1/health")

    # --- independent of the task's working directory; the Caddy result is logged ----------------------
    def test_launches_with_the_absolute_caddyfile_from_any_working_directory(self):
        """Scheduled tasks may start in C:\\Windows\\System32: the wrapper must not depend on it."""
        with FakeHealth((200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url, cwd=SYSTEM32)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_caddy_started_with_the_env_file()
        call = self.caddy_call()
        caddyfile = str(PROJECT_ROOT / "deploy" / "Caddyfile")
        self.assertIn(f'--config "{caddyfile}"', call["ARGS"])        # absolute, quoted (the path has spaces)
        self.assertEqual(call["CWD"].strip(), str(PROJECT_ROOT))       # Caddy runs in the project root, not System32
        self.assertIn(f'--config "{caddyfile}" (working directory {PROJECT_ROOT})', self.task_log())

    def test_caddy_that_stays_up_is_confirmed_running(self):
        self.caddy.write_text(FAKE_CADDY_RUNNING, encoding="ascii")
        with FakeHealth((200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url, cwd=SYSTEM32)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.task_log()
        self.assertRegex(log, r"Caddy running \(pid \d+\); serving http://localhost:8080")
        self.assertLess(log.index("Caddy running"), log.index("Caddy exited with code 0"))

    def test_caddy_failing_at_startup_is_logged_with_its_error_and_exit_code(self):
        self.caddy.write_text(FAKE_CADDY_PORT_IN_USE, encoding="ascii")
        with FakeHealth((200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url, cwd=SYSTEM32)
        self.assertEqual(result.returncode, 1)  # Task Scheduler sees the failure (and retries)
        log = self.task_log()
        self.assertIn("Caddy stopped during startup with exit code 1; last error output: "
                      "Error: loading initial config: listen tcp :8080: bind:", log)
        self.assertIn("Caddy exited with code 1", log)
        self.assertNotIn("Caddy running", log)

    def test_caddy_that_cannot_be_started_is_logged(self):
        not_a_program = self.dir / "caddy.txt"
        not_a_program.write_text("not an executable", encoding="ascii")
        with FakeHealth((200, {"status": "degraded"})) as health:
            result = self.run_wrapper(health.url, caddy=not_a_program)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Caddy could not be started:", self.task_log())

    # --- settings problems are logged immediately, without waiting ----------------------------------
    def test_missing_frontend_is_reported_without_waiting(self):
        (self.dist / "index.html").unlink()
        result = self.run_wrapper("http://127.0.0.1:9/api/v1/health", wait_seconds=600)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.caddy_call())
        self.assertIn("FRONTEND_DIST has no index.html", self.task_log())

    def test_missing_site_address_is_reported(self):
        self.env_file.write_text(f"FRONTEND_DIST={self.dist}\n", encoding="utf-8-sig")
        result = self.run_wrapper("http://127.0.0.1:9/api/v1/health", wait_seconds=600)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SITE_ADDRESS is not set", self.task_log())


if __name__ == "__main__":
    unittest.main()
