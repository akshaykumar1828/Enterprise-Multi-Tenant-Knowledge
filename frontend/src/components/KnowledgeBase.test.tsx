import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "../App";
import type { KnowledgeDocument } from "../api/types";
import { TOKEN, mockApi, type RecordedCall } from "../test/mockApi";

const doc = (id: number, filename: string, origin: "upload" | "folder"): KnowledgeDocument => ({
  id, filename, origin, source_type: origin === "upload" ? "upload" : "local", chunk_count: 3,
  ingested_at: "2026-09-30T10:00:00Z", deletable: origin === "upload",
});

/** A small stateful documents backend for the tests. */
function documentsBackend(initial: KnowledgeDocument[]) {
  const store = [...initial];
  let nextId = 100;
  return {
    store,
    routes: {
      "GET /api/v1/documents": (call: RecordedCall) => {
        const params = new URL(call.path, "http://x").searchParams;
        const limit = Number(params.get("limit")), offset = Number(params.get("offset"));
        return { status: 200, body: { total: store.length, limit, offset, items: store.slice(offset, offset + limit) } };
      },
      "POST /api/v1/documents": (call: RecordedCall) => {
        const file = (call.body as FormData).get("file") as File;
        const created = doc(nextId++, file.name, "upload");
        store.unshift(created);
        return { status: 201, body: created };
      },
      "DELETE /api/v1/documents/:id": (call: RecordedCall) => {
        const id = Number(call.path.split("/").pop());
        const index = store.findIndex((d) => d.id === id);
        if (index < 0) return { status: 404, body: { error: { code: "document_not_found", message: "Document not found." } } };
        store.splice(index, 1);
        return { status: 204, body: null };
      },
    },
  };
}

async function openKnowledgeBase(user = userEvent.setup()) {
  await user.type(screen.getByLabelText("Email"), "dev@example.com");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await user.click(await screen.findByRole("button", { name: "Knowledge base" }));
  return user;
}

describe("knowledge base", () => {
  it("lists the tenant's documents with the bearer token", async () => {
    const backend = documentsBackend([doc(2, "policy.md", "upload"), doc(1, "sample_company_handbook.md", "folder")]);
    const calls = mockApi(backend.routes);
    render(<App />);
    await openKnowledgeBase();

    expect(await screen.findByText("policy.md")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Documents \(2\)/ })).toBeInTheDocument();
    const list = calls.find((c) => c.path.startsWith("/api/v1/documents"))!;
    expect(list.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(list.path).not.toMatch(/tenant/i);
    // Uploaded documents can be deleted; folder-ingested ones are read-only here.
    expect(screen.getByRole("button", { name: "Delete policy.md" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete sample_company_handbook.md" })).not.toBeInTheDocument();
    expect(screen.getByText("Managed")).toBeInTheDocument();
  });

  it("uploads a file as multipart form data and refreshes the list", async () => {
    const backend = documentsBackend([]);
    const calls = mockApi(backend.routes);
    render(<App />);
    const user = await openKnowledgeBase();
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument();

    const file = new File(["# Notes\n\nThe code is 42."], "notes.md", { type: "text/markdown" });
    await user.upload(screen.getByLabelText("Document file"), file);
    await user.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByText(/Uploaded notes\.md \(3 chunks\)/)).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: "notes.md" })).toBeInTheDocument();
    const upload = calls.find((c) => c.method === "POST" && c.path === "/api/v1/documents")!;
    expect(upload.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(upload.headers["Content-Type"]).toBeUndefined(); // the browser sets the multipart boundary
    const form = upload.body as FormData;
    expect((form.get("file") as File).name).toBe("notes.md");
    expect([...form.keys()]).toEqual(["file"]); // no tenant, no path — only the file
  });

  it("rejects unsupported and oversized files in the browser", async () => {
    const calls = mockApi(documentsBackend([]).routes);
    render(<App />);
    const user = await openKnowledgeBase(userEvent.setup({ applyAccept: false }));
    await screen.findByText(/No documents yet/);

    await user.upload(screen.getByLabelText("Document file"), new File(["MZ"], "tool.exe"));
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Only PDF, TXT and Markdown");

    const big = new File(["x"], "big.pdf");
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("Document file"), big);
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(screen.getByRole("alert")).toHaveTextContent("at most 10 MB");
    expect(calls.filter((c) => c.method === "POST" && c.path === "/api/v1/documents")).toHaveLength(0);
  });

  it("shows the server's reason when an upload is refused", async () => {
    const backend = documentsBackend([]);
    mockApi({
      ...backend.routes,
      "POST /api/v1/documents": () => ({
        status: 422,
        body: { error: { code: "unreadable_document", message: "Could not extract text from the file." } },
      }),
    });
    render(<App />);
    const user = await openKnowledgeBase();
    await screen.findByText(/No documents yet/);
    await user.upload(screen.getByLabelText("Document file"), new File(["%PDF-1.4 broken"], "scan.pdf"));
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not extract text");
  });

  it("deletes an uploaded document after confirmation, and not without it", async () => {
    const backend = documentsBackend([doc(7, "old-policy.md", "upload"), doc(1, "sample_company_handbook.md", "folder")]);
    const calls = mockApi(backend.routes);
    render(<App />);
    const user = await openKnowledgeBase();
    const deleteButton = await screen.findByRole("button", { name: "Delete old-policy.md" });

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await user.click(deleteButton);
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);

    confirm.mockReturnValueOnce(true);
    await user.click(deleteButton);
    expect(await screen.findByText("Deleted old-policy.md.")).toBeInTheDocument();
    const deletion = calls.find((c) => c.method === "DELETE")!;
    expect(deletion.path).toBe("/api/v1/documents/7");
    expect(deletion.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    await waitFor(() => expect(screen.queryByText("old-policy.md")).not.toBeInTheDocument());
    expect(within(screen.getByRole("table")).getByText("sample_company_handbook.md")).toBeInTheDocument();
  });

  it("shows 404 on delete and reloads the list from the server", async () => {
    const backend = documentsBackend([doc(7, "moved.md", "upload"), doc(8, "kept.md", "upload")]);
    const calls = mockApi({
      ...backend.routes,
      // The document became unreadable (or was removed) after the list was loaded.
      "DELETE /api/v1/documents/:id": () => {
        backend.store.splice(0, 1);
        return { status: 404, body: { error: { code: "document_not_found", message: "Document not found." } } };
      },
    });
    render(<App />);
    const user = await openKnowledgeBase();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "Delete moved.md" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Document not found.");
    await waitFor(() => expect(screen.queryByText("moved.md")).not.toBeInTheDocument());
    expect(screen.getByText("kept.md")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "GET" && c.path.startsWith("/api/v1/documents")).length).toBe(2);
  });

  it("returns to login when the session expires", async () => {
    mockApi({
      "GET /api/v1/documents": () => ({
        status: 401, body: { error: { code: "token_expired", message: "The access token has expired; log in again." } },
      }),
    });
    render(<App />);
    await openKnowledgeBase();
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Your session has expired");
  });

  it("pages through a large knowledge base", async () => {
    const many = Array.from({ length: 45 }, (_, i) => doc(i + 1, `file-${i + 1}.md`, "folder"));
    const calls = mockApi(documentsBackend(many).routes);
    render(<App />);
    const user = await openKnowledgeBase();
    expect(await screen.findByText("1–20 of 45")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("21–40 of 45")).toBeInTheDocument();
    expect(calls.map((c) => c.path)).toContain("/api/v1/documents?limit=20&offset=20");
  });
});
