"""Per-upload resource limits: size, request body, PDF pages, extracted text, chunks.

Every rejection must happen before anything is embedded or stored, and leave no
file, document or chunk behind. Valid uploads (including at the limits) still
work, and folder ingestion is not limited. Throwaway tenant; no Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_upload_limits -v
"""

import io
import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import SAMPLE_PDF_PATH, bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient
from pypdf import PageObject, PdfReader, PdfWriter

import src.api.documents as documents_api
import src.rag.ingest as ingest
from src.api.main import app
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.loader import load_document, load_documents
from src.rag.tenants import get_or_create_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS = "/api/v1/documents"
SAMPLE_PDF = SAMPLE_PDF_PATH.read_bytes()
SAMPLE_PDF_PAGES = len(PdfReader(SAMPLE_PDF_PATH).pages)
SAMPLE_PDF_CHARS = sum(len(p.text) for p in load_document(SAMPLE_PDF_PATH).pages)
UNLIMITED_QUOTAS = {"TENANT_MAX_DOCUMENTS": "0", "TENANT_MAX_UPLOAD_BYTES": "0"}


def sections(count: int) -> bytes:
    """A Markdown file that chunks into exactly `count` chunks (one short section each)."""
    return "".join(f"# Section {i}\n\nThe Kite fact number {i} is {secrets.token_hex(3)}.\n\n"
                   for i in range(count)).encode()


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class UploadLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="rag-upload-limits-")
        cls.upload_dir = Path(cls.sandbox) / "uploads"
        cls.env = mock.patch.dict(os.environ, {"UPLOAD_DIR": str(cls.upload_dir), **UNLIMITED_QUOTAS})
        cls.env.start()
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.conn = connect()
        cls.tenant = get_or_create_tenant(cls.conn, f"test-upload-limits-{secrets.token_hex(4)}", "Upload limits").id
        user, password = make_user(cls.conn, cls.tenant, "upload-limits")  # an ordinary employee
        cls.token = login(cls.client, user.email, password)

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = %s", (cls.tenant,))
        cls.conn.execute("DELETE FROM tenants WHERE id = %s", (cls.tenant,))
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        cls.env.stop()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def tearDown(self):
        for (stored,) in self.conn.execute("DELETE FROM documents WHERE tenant_id = %s RETURNING path", (self.tenant,)):
            Path(stored).unlink(missing_ok=True)

    # --- helpers ---------------------------------------------------------------------------------
    def upload(self, name: str, data: bytes):
        return self.client.post(DOCS, headers=bearer(self.token), files={"file": (name, data, "application/octet-stream")})

    def assert_rejected_cleanly(self, response, status: int, code: str):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertNotRegex(error["message"], r"[A-Za-z]:\\|/tmp|uploads[\\/]|\.partial|Traceback")
        # Nothing left behind: no file (final or partial), no document, no chunk.
        self.assertEqual([p for p in self.upload_dir.rglob("*") if p.is_file()], [])
        self.assertEqual(self.conn.execute(
            "SELECT (SELECT count(*) FROM documents WHERE tenant_id = %s),"
            "       (SELECT count(*) FROM document_chunks WHERE tenant_id = %s)", (self.tenant, self.tenant)).fetchone(),
            (0, 0))
        return error

    # --- file size and request body --------------------------------------------------------------------
    def test_file_larger_than_the_maximum_is_refused(self):
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_BYTES": "2000"}):
            error = self.assert_rejected_cleanly(self.upload("big.md", b"# Big\n\n" + b"word " * 1000), 413, "file_too_large")
        self.assertIn("at most", error["message"])

    def test_request_body_without_content_length_is_capped(self):
        """Chunked transfer (no Content-Length) skips the header check; the body counter still stops it."""
        boundary = "limitboundary" + secrets.token_hex(4)
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"stream.md\"\r\n"
                "Content-Type: text/markdown\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        def body(size: int):
            yield head
            for _ in range(size // 8192):
                yield b"word " * 1638 + b"xx"  # 8,192 bytes
            yield tail

        headers = {**bearer(self.token), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_BYTES": "4096"}):
            with mock.patch.object(ingest, "embed_texts", wraps=ingest.embed_texts) as embed, \
                    mock.patch.object(documents_api, "ingest_upload", wraps=documents_api.ingest_upload) as route:
                response = self.client.post(DOCS, headers=headers, content=body(512 * 1024))
            self.assertNotIn("content-length", {k.lower() for k in response.request.headers})
            self.assert_rejected_cleanly(response, 413, "file_too_large")
            # Stopped while the body was being received: the route never got to the upload.
            route.assert_not_called()
            embed.assert_not_called()
        # A small streamed upload still works.
        small = self.client.post(DOCS, headers=headers, content=iter([head, b"# Streamed\n\nThe Kite code is 7.\n", tail]))
        self.assertEqual(small.status_code, 201, small.text)

    # --- PDF pages -----------------------------------------------------------------------------------
    def test_pdf_with_too_many_pages_is_refused_before_extraction(self):
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_PDF_PAGES": str(SAMPLE_PDF_PAGES - 1)}):
            with mock.patch.object(PageObject, "extract_text", autospec=True, return_value="x") as extract:
                self.assert_rejected_cleanly(self.upload("policy.pdf", SAMPLE_PDF), 413, "too_many_pages")
                extract.assert_not_called()
            # Blank pages count too: a page-heavy PDF is refused regardless of its text.
            self.assert_rejected_cleanly(self.upload("blank.pdf", blank_pdf(SAMPLE_PDF_PAGES + 5)), 413, "too_many_pages")
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_PDF_PAGES": str(SAMPLE_PDF_PAGES)}):
            self.assertEqual(self.upload("policy.pdf", SAMPLE_PDF).status_code, 201)  # exactly at the limit

    # --- extracted text -----------------------------------------------------------------------------------
    def test_too_much_extracted_text_is_refused(self):
        text = b"# Notes\n\n" + b"The Kite ledger grows. " * 200  # ~4,600 characters
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_TEXT_CHARS": "1000"}):
            with mock.patch.object(ingest, "embed_texts", wraps=ingest.embed_texts) as embed:
                self.assert_rejected_cleanly(self.upload("notes.md", text), 413, "too_much_text")
                embed.assert_not_called()

    def test_pdf_text_limit_stops_extraction_early(self):
        first_page_chars = len(load_document(SAMPLE_PDF_PATH).pages[0].text)
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_TEXT_CHARS": str(first_page_chars)}):
            with mock.patch.object(PageObject, "extract_text", autospec=True,
                                   side_effect=PageObject.extract_text) as extract:
                self.assert_rejected_cleanly(self.upload("policy.pdf", SAMPLE_PDF), 413, "too_much_text")
            self.assertLess(extract.call_count, SAMPLE_PDF_PAGES)  # later pages were never extracted
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_TEXT_CHARS": str(SAMPLE_PDF_CHARS)}):
            self.assertEqual(self.upload("policy.pdf", SAMPLE_PDF).status_code, 201)  # exactly at the limit

    # --- chunks -------------------------------------------------------------------------------------------
    def test_too_many_chunks_is_refused_before_embedding(self):
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_CHUNKS": "10"}):
            with mock.patch.object(ingest, "embed_texts", wraps=ingest.embed_texts) as embed:
                error = self.assert_rejected_cleanly(self.upload("many.md", sections(11)), 413, "too_many_chunks")
                embed.assert_not_called()
            self.assertIn("10", error["message"])
            ok = self.upload("ten.md", sections(10))  # exactly at the limit
            self.assertEqual((ok.status_code, ok.json()["chunk_count"]), (201, 10))

    # --- valid uploads with the defaults ----------------------------------------------------------------
    def test_valid_uploads_still_succeed_with_default_limits(self):
        for key in ("UPLOAD_MAX_BYTES", "UPLOAD_MAX_PDF_PAGES", "UPLOAD_MAX_TEXT_CHARS", "UPLOAD_MAX_CHUNKS"):
            self.assertNotIn(key, os.environ)
        for name, data in (("handbook.md", sections(40)), ("policy.pdf", SAMPLE_PDF),
                           ("notes.txt", b"Kite notes\n\nThe Kite archive opens at nine.\n")):
            with self.subTest(file=name):
                response = self.upload(name, data)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertGreater(response.json()["chunk_count"], 0)

    def test_unreadable_file_error_reveals_no_server_details(self):
        error = self.assert_rejected_cleanly(self.upload("broken.pdf", b"%PDF-1.4\nnot really a pdf\n%%EOF"),
                                             422, "unreadable_document")
        self.assertIn("Could not extract text", error["message"])
        self.assertNotIn(str(self.upload_dir), error["message"])

    # --- configuration --------------------------------------------------------------------------------
    def test_invalid_limit_settings_stop_the_api_at_startup(self):
        for name in ("UPLOAD_MAX_BYTES", "UPLOAD_MAX_PDF_PAGES", "UPLOAD_MAX_TEXT_CHARS", "UPLOAD_MAX_CHUNKS"):
            for value in ("0", "-5", "many"):
                with self.subTest(setting=name, value=value), mock.patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        with TestClient(app):
                            pass

    # --- folder ingestion is not limited ------------------------------------------------------------------
    def test_folder_ingestion_is_unaffected(self):
        folder = Path(self.sandbox) / "folder"
        folder.mkdir(exist_ok=True)
        path = folder / "long.md"
        path.write_bytes(sections(30) + b"The Kite ledger grows. " * 100)
        (folder / "policy.pdf").write_bytes(SAMPLE_PDF)
        # Tiny upload limits are configured, but folder ingestion (load_documents + chunk_document,
        # as src.rag.ingest calls them) passes no limits and is not affected.
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_TEXT_CHARS": "10", "UPLOAD_MAX_CHUNKS": "1", "UPLOAD_MAX_PDF_PAGES": "1"}):
            documents = {d.relative_path: d for d in load_documents(folder)}
            self.assertGreater(len(chunk_document(documents["long.md"])), 30)
            self.assertEqual(len(documents["policy.pdf"].pages), len(load_document(folder / "policy.pdf").pages))
            self.assertGreater(len(chunk_document(documents["policy.pdf"])), 1)


if __name__ == "__main__":
    unittest.main()
