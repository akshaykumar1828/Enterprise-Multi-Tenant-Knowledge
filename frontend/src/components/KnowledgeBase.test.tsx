import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

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
  await user.click(await screen.findByRole("button", { name: "Documents" }));
  return user;
}

/** Answer the confirmation dialog. */
async function answerDialog(user: ReturnType<typeof userEvent.setup>, button: "Cancel" | "Delete document") {
  const dialog = await screen.findByRole("alertdialog", { name: "Delete this document?" });
  await user.click(within(dialog).getByRole("button", { name: button }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
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

    expect(await screen.findByText(/Uploaded notes\.md\. It can now be used to answer questions/)).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: /notes\.md/ })).toBeInTheDocument();
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

    await user.click(deleteButton);
    await answerDialog(user, "Cancel");
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
    expect(deleteButton).toHaveFocus(); // focus returns to where it was

    await user.click(deleteButton);
    await answerDialog(user, "Delete document");
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
    await user.click(await screen.findByRole("button", { name: "Delete moved.md" }));
    await answerDialog(user, "Delete document");
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

  it("shows each document's title, a short description beneath it, and the file name as secondary text", async () => {
    const handbook = {
      ...doc(1, "sample_company_handbook.md", "folder"),
      description: "Employees receive 20 days of annual paid leave per calendar year, with rules for requesting leave.",
    };
    const bench = {
      ...doc(2, "dsid_0123456789abcdef0123456789abcdef__q3-pricing-review-notes.txt", "folder"),
      source_type: "confluence",
      description: "Notes from the quarterly pricing review, covering discount bands and open decisions.",
    };
    const noText = doc(3, "empty-scan.pdf", "upload"); // no description available
    mockApi(documentsBackend([handbook, bench, noText]).routes);
    render(<App />);
    await openKnowledgeBase();

    // 1./2. Title, then the description directly below it.
    const title = await screen.findByText("Sample company handbook");
    const description = screen.getByText(handbook.description);
    expect(title.nextElementSibling).toBe(description);
    expect(description.tagName).toBe("P");
    expect(description).toHaveClass("doc-description");
    // 3. The file name stays available, after the description and de-emphasized.
    const file = screen.getByText("sample_company_handbook.md");
    expect(description.nextElementSibling).toBe(file);
    expect(file).toHaveClass("doc-file");
    // Internal benchmark ids never appear in the title or description, only in the secondary file name.
    expect(screen.getByText("Q3 pricing review notes")).toBeInTheDocument();
    expect(screen.getByText(bench.description)).not.toHaveTextContent("dsid_");
    // A document without a description simply has none (no placeholder text invented).
    const emptyRow = screen.getByText("empty-scan.pdf").closest("td")!;
    expect(within(emptyRow).queryByText((_, el) => el?.classList.contains("doc-description") ?? false)).toBeNull();
  });

  it("only shows descriptions of documents the server listed for this user", async () => {
    // The list endpoint is already filtered by the server; a restricted document is simply not in it,
    // so its description cannot appear anywhere. The UI never requests descriptions separately.
    const visible = { ...doc(1, "company-policy.md", "folder"), description: "Company-wide travel policy for all employees." };
    const calls = mockApi(documentsBackend([visible]).routes);
    render(<App />);
    await openKnowledgeBase();
    expect(await screen.findByText(visible.description)).toBeInTheDocument();
    expect(screen.queryByText(/Board compensation decisions/)).not.toBeInTheDocument();
    const paths = calls.map((c) => c.path);
    expect(paths.filter((p) => !p.startsWith("/api/v1/auth/")).every((p) => p.startsWith("/api/v1/documents?limit="))).toBe(true);
  });

  it("keeps long descriptions visually constrained", async () => {
    const long = Array.from({ length: 60 }, (_, i) => `word${i}`).join(" ") + ".";
    mockApi(documentsBackend([{ ...doc(1, "long.md", "folder"), description: long }]).routes);
    render(<App />);
    await openKnowledgeBase();
    const description = await screen.findByText(/^word0 word1/);
    expect(description).toHaveClass("doc-description"); // CSS clamps it to two lines
    expect(description.textContent!.length).toBeLessThanOrEqual(240);
    expect(description.textContent!.endsWith("…")).toBe(true);
    expect(description.textContent).not.toContain("word59");
  });

  it("searches across every accessible document, not just the current page", async () => {
    const many = Array.from({ length: 45 }, (_, i) => doc(i + 1, `file-${i + 1}.md`, "folder"));
    const calls = mockApi(documentsBackend(many).routes);
    render(<App />);
    const user = await openKnowledgeBase();
    await screen.findByText("1–20 of 45");

    await user.type(screen.getByLabelText("Search documents"), "file 44");
    expect(await screen.findByText("1 matching document")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("file-44.md")).toBeInTheDocument();
    expect(screen.queryByText("1–20 of 45")).not.toBeInTheDocument(); // no pager while searching
    // The list is read with the normal (server-filtered) endpoint; nothing else is sent.
    const searchCalls = calls.filter((c) => c.path.startsWith("/api/v1/documents?limit=200"));
    expect(searchCalls.length).toBeGreaterThanOrEqual(1);
    expect(searchCalls.every((c) => !/tenant|q=|search/i.test(c.path))).toBe(true);

    await user.clear(screen.getByLabelText("Search documents"));
    expect(await screen.findByText("1–20 of 45")).toBeInTheDocument();
  });
});
