import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "../../App";
import type { CompanyUser, CurrentUser, Department, DocumentAccess, KnowledgeDocument } from "../../api/types";
import { ADMIN_USER, TOKEN, USER, mockApi, type RecordedCall } from "../../test/mockApi";

const STORAGE_KEY = "ek.accessToken";
type Reply = { status: number; body: unknown };
const error = (status: number, code: string, message: string): Reply => ({ status, body: { error: { code, message } } });
const idAt = (call: RecordedCall, fromEnd: number) => Number(call.path.split("?")[0].split("/").at(-fromEnd));

/** A small stateful Admin API that behaves like the server (admin-only via `me.role`). */
function adminBackend(me: CurrentUser = ADMIN_USER) {
  const state = {
    me,
    departments: [
      { id: 10, slug: "finance", name: "Finance", member_count: 1, document_count: 0 },
      { id: 11, slug: "legal", name: "Legal", member_count: 0, document_count: 0 },
    ] as Department[],
    users: [
      { id: 1, email: "dev@example.com", display_name: "Dev User", role: me.role, department_ids: [], created_at: "2026-09-30T10:00:00Z" },
      { id: 2, email: "alice@example.com", display_name: "Alice", role: "employee", department_ids: [10], created_at: "2026-09-30T10:00:00Z" },
    ] as CompanyUser[],
    documents: [
      { id: 5, filename: "budget.md", source_type: "upload", origin: "upload", chunk_count: 2, ingested_at: "2026-09-30T10:00:00Z", deletable: true },
    ] as KnowledgeDocument[],
    access: { 5: { id: 5, filename: "budget.md", origin: "upload", visibility: "company", department_ids: [] } } as Record<number, DocumentAccess>,
  };
  const admin = (handler: (call: RecordedCall) => Reply) => (call: RecordedCall): Reply =>
    state.me.role === "admin" ? handler(call) : error(403, "admin_required", "This action requires an administrator.");
  const user = (call: RecordedCall, fromEnd: number) => state.users.find((u) => u.id === idAt(call, fromEnd));

  const routes: Record<string, (call: RecordedCall) => Reply> = {
    "GET /api/v1/auth/me": () => ({ status: 200, body: state.me }),
    "GET /api/v1/documents": () => ({ status: 200, body: { total: state.documents.length, limit: 20, offset: 0, items: state.documents } }),
    "GET /api/v1/admin/departments": admin(() => ({ status: 200, body: state.departments })),
    "POST /api/v1/admin/departments": admin((call) => {
      const { slug, name } = call.body as { slug: string; name: string };
      if (state.departments.some((d) => d.slug === slug)) return error(409, "department_exists", "A department with this slug already exists.");
      const created = { id: 12, slug, name, member_count: 0, document_count: 0 };
      state.departments.push(created);
      return { status: 201, body: created };
    }),
    "PATCH /api/v1/admin/departments/:id": admin((call) => {
      const department = state.departments.find((d) => d.id === idAt(call, 1));
      if (!department) return error(404, "department_not_found", "Department not found.");
      Object.assign(department, call.body);
      return { status: 200, body: department };
    }),
    "DELETE /api/v1/admin/departments/:id": admin((call) => {
      const id = idAt(call, 1);
      if (id === 10) return error(409, "department_in_use", "This department is assigned to 1 document(s). Change those documents' access first.");
      state.departments = state.departments.filter((d) => d.id !== id);
      return { status: 204, body: null };
    }),
    "GET /api/v1/admin/users": admin(() => ({ status: 200, body: { total: state.users.length, limit: 50, offset: 0, items: state.users } })),
  };
  // Parameterized user and document routes (matched by the fetch mock below).
  const dynamic = (call: RecordedCall): Reply | null => {
    const path = call.path.split("?")[0];
    if (call.method === "PUT" && /^\/api\/v1\/admin\/users\/\d+\/role$/.test(path)) {
      return admin(() => {
        const target = user(call, 2)!;
        const role = (call.body as { role: "admin" | "employee" }).role;
        if (target.role === "admin" && role === "employee" && state.users.filter((u) => u.role === "admin").length === 1) {
          return error(409, "last_admin", "The company must keep at least one admin.");
        }
        target.role = role;
        if (target.id === state.me.id) state.me = { ...state.me, role };
        return { status: 200, body: target };
      })(call);
    }
    if (/^\/api\/v1\/admin\/users\/\d+\/departments\/\d+$/.test(path)) {
      return admin(() => {
        const target = user(call, 3)!;
        const departmentId = idAt(call, 1);
        target.department_ids = call.method === "PUT"
          ? [...new Set([...target.department_ids, departmentId])].sort()
          : target.department_ids.filter((id) => id !== departmentId);
        return { status: 200, body: target };
      })(call);
    }
    if (/^\/api\/v1\/admin\/documents\/\d+\/access$/.test(path)) {
      return admin(() => {
        const current = state.access[idAt(call, 2)];
        if (!current) return error(404, "document_not_found", "Document not found.");
        if (call.method === "PUT") Object.assign(current, call.body);
        return { status: 200, body: current };
      })(call);
    }
    return null;
  };
  return { state, routes, dynamic };
}

/** mockApi with the admin backend's dynamic routes in front of the static ones. */
function mockAdmin(backend: ReturnType<typeof adminBackend>, overrides: Record<string, (call: RecordedCall) => Reply> = {}) {
  const calls = mockApi({ ...backend.routes, ...overrides });
  const staticFetch = vi.mocked(globalThis.fetch).getMockImplementation()!;
  vi.mocked(globalThis.fetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const raw = init?.body;
    const call: RecordedCall = {
      method: init?.method ?? "GET", path: url.pathname + url.search,
      headers: { ...(init?.headers as Record<string, string>) }, body: raw ? JSON.parse(String(raw)) : undefined,
    };
    const reply = backend.dynamic(call);
    if (!reply) return staticFetch(input, init);
    calls.push(call);
    return new Response(reply.status === 204 ? null : JSON.stringify(reply.body), { status: reply.status });
  });
  return calls;
}

async function logIn(user = userEvent.setup()) {
  await user.type(screen.getByLabelText("Email"), "dev@example.com");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("button", { name: "Chat" });
  return user;
}

async function openAdmin(view?: "Users" | "Document access") {
  const user = await logIn();
  await user.click(screen.getByRole("button", { name: "Admin" }));
  await screen.findByRole("heading", { name: "Departments" });
  if (view) await user.click(screen.getByRole("button", { name: view }));
  return user;
}

const adminCalls = (calls: RecordedCall[]) => calls.filter((c) => c.path.startsWith("/api/v1/admin"));

describe("admin section visibility", () => {
  it("employees see chat, knowledge base and upload, but no admin navigation or controls", async () => {
    const calls = mockAdmin(adminBackend(USER));
    render(<App />);
    const user = await logIn();
    expect(screen.queryByRole("button", { name: "Admin" })).not.toBeInTheDocument();
    expect(screen.getByText(/Employee/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Knowledge base" }));
    expect(await screen.findByRole("button", { name: "Upload" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Manage access/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Departments|Users/ })).not.toBeInTheDocument();
    expect(adminCalls(calls)).toHaveLength(0);
  });

  it("admins see the admin navigation, and the role comes from /auth/me on reload", async () => {
    window.localStorage.setItem(STORAGE_KEY, TOKEN);
    const calls = mockAdmin(adminBackend(ADMIN_USER));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Admin" }));
    expect(await screen.findByRole("heading", { name: "Departments" })).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: "Finance" })).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/auth/me")!.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(adminCalls(calls).every((c) => c.headers.Authorization === `Bearer ${TOKEN}`)).toBe(true);
  });

  it("a stored token for an employee restores no admin section", async () => {
    window.localStorage.setItem(STORAGE_KEY, TOKEN);
    mockAdmin(adminBackend(USER));
    render(<App />);
    expect(await screen.findByRole("button", { name: "Chat" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Admin" })).not.toBeInTheDocument();
  });
});

describe("departments", () => {
  it("creates, renames and deletes departments with the right API calls", async () => {
    const backend = adminBackend();
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.type(screen.getByLabelText("Name"), "Human Resources");
    await user.type(screen.getByLabelText("Slug"), "hr");
    await user.click(screen.getByRole("button", { name: "Create department" }));
    expect(await screen.findByText("Created Human Resources.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/v1/admin/departments")!.body)
      .toEqual({ slug: "hr", name: "Human Resources" });
    expect(await screen.findByRole("cell", { name: "Human Resources" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Rename Legal" }));
    const input = screen.getByLabelText("New name");
    await user.clear(input);
    await user.type(input, "Legal & Compliance");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("cell", { name: "Legal & Compliance" })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({ path: "/api/v1/admin/departments/11", body: { name: "Legal & Compliance" } });

    await user.click(screen.getByRole("button", { name: "Delete Legal & Compliance" }));
    await waitFor(() => expect(screen.queryByRole("cell", { name: "Legal & Compliance" })).not.toBeInTheDocument());
    expect(calls.find((c) => c.method === "DELETE")!.path).toBe("/api/v1/admin/departments/11");
  });

  it("shows the server's 409 message (department in use, duplicate slug)", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "Delete Finance" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("assigned to 1 document(s)");
    expect(screen.getByRole("cell", { name: "Finance" })).toBeInTheDocument();

    await user.type(screen.getByLabelText("Name"), "Money");
    await user.type(screen.getByLabelText("Slug"), "finance");
    await user.click(screen.getByRole("button", { name: "Create department" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("slug already exists");
  });

  it("shows 422 validation messages from the server", async () => {
    mockAdmin(adminBackend(), {
      "POST /api/v1/admin/departments": () => ({
        status: 422, body: { detail: [{ loc: ["body", "slug"], msg: "String should match pattern '^[a-z0-9][a-z0-9-]{0,62}$'" }] },
      }),
    });
    render(<App />);
    const user = await openAdmin();
    await user.type(screen.getByLabelText("Name"), "Bad");
    await user.type(screen.getByLabelText("Slug"), "Bad Slug");
    await user.click(screen.getByRole("button", { name: "Create department" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("slug: String should match pattern");
  });
});

describe("users", () => {
  it("changes a role and adds/removes department memberships", async () => {
    const backend = adminBackend();
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Users");
    const row = (await screen.findByText("Alice")).closest("tr")!;
    expect(within(row).getByText("Finance")).toBeInTheDocument();

    await user.selectOptions(within(row).getByLabelText("Role for Alice"), "admin");
    expect(await screen.findByText("Alice is now an admin.")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/admin/users/2/role")).toMatchObject({ method: "PUT", body: { role: "admin" } });

    await user.selectOptions(within(row).getByLabelText("Add Alice to a department"), "11");
    await user.click(within(row).getByRole("button", { name: "Add Alice to the selected department" }));
    expect(await screen.findByText("Added Alice to Legal.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT" && c.path === "/api/v1/admin/users/2/departments/11")).toBeDefined();

    await user.click(within(row).getByRole("button", { name: "Remove Alice from Finance" }));
    expect(await screen.findByText("Removed Alice from Finance.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "DELETE" && c.path === "/api/v1/admin/users/2/departments/10")).toBeDefined();
    expect(backend.state.users[1].department_ids).toEqual([11]);
  });

  it("shows the last-admin refusal from the server", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Users");
    const row = within(await screen.findByRole("table")).getByText("Dev User").closest("tr")!;
    await user.selectOptions(within(row).getByLabelText("Role for Dev User"), "employee");
    expect(await screen.findByRole("alert")).toHaveTextContent("must keep at least one admin");
    expect(screen.getByRole("button", { name: "Admin" })).toBeInTheDocument();
  });

  it("demoting yourself removes the admin section after re-reading /auth/me", async () => {
    const backend = adminBackend();
    backend.state.users[1].role = "admin"; // a second admin exists
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Users");
    const row = within(await screen.findByRole("table")).getByText("Dev User").closest("tr")!;
    await user.selectOptions(within(row).getByLabelText("Role for Dev User"), "employee");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Admin" })).not.toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Users" })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/v1/auth/me").length).toBeGreaterThanOrEqual(2);
  });
});

describe("document access", () => {
  it("shows the current access and saves department-restricted and company-wide access", async () => {
    const backend = adminBackend();
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Document access");
    await user.click(await screen.findByRole("button", { name: "Manage access for budget.md" }));
    const editor = await screen.findByRole("form", { name: "Access for budget.md" });
    expect(within(editor).getByLabelText("Everyone in the company")).toBeChecked();
    expect(calls.find((c) => c.path === "/api/v1/admin/documents/5/access")!.method).toBe("GET");

    await user.click(within(editor).getByLabelText("Only selected departments (and admins)"));
    expect(within(editor).getByRole("note")).toHaveTextContent("only administrators can read it");
    await user.click(within(editor).getByLabelText("Finance"));
    await user.click(within(editor).getByRole("button", { name: "Save access" }));
    expect(await screen.findByText("Saved access for budget.md.")).toBeInTheDocument();
    const puts = () => calls.filter((c) => c.method === "PUT" && c.path === "/api/v1/admin/documents/5/access");
    expect(puts()[0].body).toEqual({ visibility: "departments", department_ids: [10] });

    await user.click(within(editor).getByLabelText("Everyone in the company"));
    await user.click(within(editor).getByRole("button", { name: "Save access" }));
    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(puts()[1].body).toEqual({ visibility: "company", department_ids: [] });
    expect(backend.state.access[5]).toMatchObject({ visibility: "company", department_ids: [] });
  });

  it("shows 404 when the document no longer exists", async () => {
    const backend = adminBackend();
    delete backend.state.access[5];
    mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Document access");
    await user.click(await screen.findByRole("button", { name: "Manage access for budget.md" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Document not found.");
  });
});

describe("403 from an admin endpoint", () => {
  it("shows a permission message and removes the admin section when /auth/me says employee", async () => {
    const backend = adminBackend();
    mockAdmin(backend);
    render(<App />);
    const user = await openAdmin();
    expect(await screen.findByRole("cell", { name: "Finance" })).toBeInTheDocument();

    backend.state.me = { ...ADMIN_USER, role: "employee" }; // demoted elsewhere; this UI still shows admin
    await user.type(screen.getByLabelText("Name"), "Ops");
    await user.type(screen.getByLabelText("Slug"), "ops");
    await user.click(screen.getByRole("button", { name: "Create department" }));

    // The server refuses, the UI re-reads /auth/me and drops the admin section.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Admin" })).not.toBeInTheDocument());
    expect(backend.state.departments.map((d) => d.slug)).not.toContain("ops");
    expect(screen.getByText(/Employee/)).toBeInTheDocument();
  });

  it("the permission message is shown while the section is still open", async () => {
    const backend = adminBackend();
    // /auth/me keeps saying admin, but the admin endpoint refuses (e.g. a stale role).
    mockAdmin(backend, { "GET /api/v1/admin/users": () => error(403, "admin_required", "This action requires an administrator.") });
    render(<App />);
    await openAdmin("Users");
    expect(await screen.findByRole("alert")).toHaveTextContent("You don't have permission to do this.");
  });

  it("never sends tenant, role or admin flags to decide access", async () => {
    const calls = mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Users");
    const row = (await screen.findByText("Alice")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Remove Alice from Finance" }));
    await screen.findByText("Removed Alice from Finance.");
    for (const call of calls) {
      expect(call.path).not.toMatch(/tenant|is_admin/i);
      const body = JSON.stringify(call.body ?? {});
      expect(body).not.toMatch(/tenant|is_admin/i);
      // A role is only ever sent as the new value in a role change.
      if (body.includes('"role"')) expect(call.path).toMatch(/\/admin\/users\/\d+\/role$/);
    }
  });
});
