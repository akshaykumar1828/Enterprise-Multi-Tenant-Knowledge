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
    "DELETE /api/v1/admin/users/:id": admin((call) => {
      const id = idAt(call, 1);
      if (id === state.me.id) return error(409, "cannot_delete_self", "You cannot delete your own account.");
      if (!state.users.some((u) => u.id === id)) return error(404, "user_not_found", "User not found.");
      state.users = state.users.filter((u) => u.id !== id);
      return { status: 204, body: null };
    }),
    "GET /api/v1/admin/users": admin(() => ({ status: 200, body: { total: state.users.length, limit: 50, offset: 0, items: state.users } })),
    "POST /api/v1/admin/users": admin((call) => {
      const body = call.body as { email: string; password: string; display_name?: string; department_ids: number[] };
      if (state.users.some((u) => u.email === body.email)) {
        return error(409, "email_already_registered", "An account with this email already exists.");
      }
      const created: CompanyUser = { id: 3 + state.users.length, email: body.email, display_name: body.display_name ?? null,
                                     role: "employee", department_ids: body.department_ids, created_at: "2026-10-03T10:00:00Z" };
      state.users.push(created);
      return { status: 201, body: created };
    }),
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

async function openAdmin(view?: "Users" | "Document Access") {
  const user = await logIn();
  await user.click(screen.getByRole("button", { name: "Admin" }));
  await screen.findByRole("heading", { name: "Departments" });
  if (view) await user.click(screen.getByRole("tab", { name: view }));
  return user;
}

/** Open the "New department" form and fill it (the slug is suggested from the name; replace it if given). */
async function fillNewDepartment(user: ReturnType<typeof userEvent.setup>, name: string, slug?: string) {
  await user.click(screen.getByRole("button", { name: "New department" }));
  await user.type(screen.getByLabelText("Name"), name);
  if (slug !== undefined) {
    await user.clear(screen.getByLabelText("Slug"));
    await user.type(screen.getByLabelText("Slug"), slug);
  }
}

/** Answer a confirmation dialog. */
async function answerDialog(user: ReturnType<typeof userEvent.setup>, title: RegExp, button: string) {
  const dialog = await screen.findByRole("alertdialog", { name: title });
  await user.click(within(dialog).getByRole("button", { name: button }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
}

const adminCalls = (calls: RecordedCall[]) => calls.filter((c) => c.path.startsWith("/api/v1/admin"));

describe("admin section visibility", () => {
  it("employees see chat, knowledge base and upload, but no admin navigation or controls", async () => {
    const calls = mockAdmin(adminBackend(USER));
    render(<App />);
    const user = await logIn();
    expect(screen.queryByRole("button", { name: "Admin" })).not.toBeInTheDocument();
    expect(screen.getByText(/Employee/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Documents" }));
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

  it("admin sections are keyboard-navigable tabs", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin();
    const departments = screen.getByRole("tab", { name: "Departments" });
    expect(departments).toHaveAttribute("aria-selected", "true");
    departments.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Users" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Users" })).toHaveFocus();
    expect(await screen.findByRole("heading", { name: /Users/ })).toBeInTheDocument();
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Document Access" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Departments" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Document Access" })).toHaveFocus();
  });

  it("shows exactly three tabs, Departments first, each with its own panel", async () => {
    const calls = mockAdmin(adminBackend());
    render(<App />);
    await openAdmin();
    const tabs = within(screen.getByRole("tablist", { name: "Administration sections" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Departments", "Users", "Document Access"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    for (const tab of tabs) {
      const panel = document.getElementById(tab.getAttribute("aria-controls")!)!;
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
    }
    expect(screen.getByRole("tabpanel", { name: "Departments" })).toBeVisible();
    // Users and Document Access load nothing until they are opened.
    expect(calls.some((c) => c.path.startsWith("/api/v1/admin/users"))).toBe(false);
    expect(calls.some((c) => c.path.startsWith("/api/v1/documents"))).toBe(false);
  });

  it("renders the Users and Document Access screens", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Users");
    const usersPanel = screen.getByRole("tabpanel", { name: "Users" });
    expect(await within(usersPanel).findByRole("heading", { name: /Users \(2\)/ })).toBeInTheDocument();
    expect(within(usersPanel).getByRole("columnheader", { name: "Role" })).toBeInTheDocument();
    expect(within(usersPanel).getByText("alice@example.com")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Document Access" }));
    const accessPanel = screen.getByRole("tabpanel", { name: "Document Access" });
    expect(within(accessPanel).getByRole("heading", { name: "Document access" })).toBeInTheDocument();
    expect(await within(accessPanel).findByRole("button", { name: "Manage access for budget.md" })).toBeInTheDocument();
    expect(within(accessPanel).getByLabelText("Search documents")).toBeInTheDocument();
  });

  it("keeps each tab's state when switching away and back", async () => {
    const calls = mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Document Access");
    await user.click(await screen.findByRole("button", { name: "Manage access for budget.md" }));
    await screen.findByRole("form", { name: "Access for budget.md" });
    await user.click(screen.getByLabelText("Only selected departments (and admins)")); // unsaved choice

    await user.click(screen.getByRole("tab", { name: "Users" }));
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Document Access" }));

    const editor = screen.getByRole("form", { name: "Access for budget.md" }); // still open
    expect(within(editor).getByLabelText("Only selected departments (and admins)")).toBeChecked();
    expect(calls.filter((c) => c.path === "/api/v1/admin/documents/5/access")).toHaveLength(1); // not re-fetched
    expect(adminCalls(calls).filter((c) => c.method !== "GET")).toHaveLength(0); // switching tabs changes nothing
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

    await user.click(screen.getByRole("button", { name: "New department" }));
    await user.type(screen.getByLabelText("Name"), "Human Resources");
    expect(screen.getByLabelText("Slug")).toHaveValue("human-resources"); // suggested from the name
    await user.clear(screen.getByLabelText("Slug"));
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

    // Deleting asks first; Cancel keeps the department.
    await user.click(screen.getByRole("button", { name: "Delete Legal & Compliance" }));
    await answerDialog(user, /Delete Legal & Compliance\?/, "Cancel");
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Delete Legal & Compliance" }));
    await answerDialog(user, /Delete Legal & Compliance\?/, "Delete department");
    await waitFor(() => expect(screen.queryByRole("cell", { name: "Legal & Compliance" })).not.toBeInTheDocument());
    expect(calls.find((c) => c.method === "DELETE")!.path).toBe("/api/v1/admin/departments/11");
  });

  it("shows the server's 409 message (department in use, duplicate slug)", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin();
    await user.click(await screen.findByRole("button", { name: "Delete Finance" }));
    await answerDialog(user, /Delete Finance\?/, "Delete department");
    expect(await screen.findByRole("alert")).toHaveTextContent("assigned to 1 document(s)");
    expect(screen.getByRole("cell", { name: "Finance" })).toBeInTheDocument();

    await fillNewDepartment(user, "Money", "finance");
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
    await fillNewDepartment(user, "Bad", "Bad Slug");
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

  it("adds an employee with an initial password and departments", async () => {
    const backend = adminBackend();
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Users");
    await screen.findByText("Alice");
    await user.click(screen.getByRole("button", { name: "Add employee" }));
    const form = screen.getByRole("form", { name: "Add employee" });
    const password = within(form).getByLabelText("Initial password") as HTMLInputElement;
    expect(password.value.length).toBeGreaterThanOrEqual(12); // a generated password is filled in
    const generated = password.value;
    await user.click(within(form).getByRole("button", { name: "Generate" }));
    expect(password.value).not.toBe(generated);

    await user.type(within(form).getByLabelText(/Full name/), "Ravi Kumar");
    await user.type(within(form).getByLabelText("Work email"), "ravi@example.com");
    await user.click(within(form).getByLabelText("Finance"));
    await user.click(within(form).getByRole("button", { name: "Add employee" }));

    expect(await screen.findByText(/Added Ravi Kumar\. They can now log in with ravi@example\.com/)).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.path === "/api/v1/admin/users")!;
    expect(post.body).toEqual({ email: "ravi@example.com", password: password.value, display_name: "Ravi Kumar",
                                department_ids: [10] });
    expect(JSON.stringify(post.body)).not.toMatch(/tenant|role|admin/i); // the server decides tenant and role
    expect(screen.queryByRole("form", { name: "Add employee" })).not.toBeInTheDocument();
    const row = (await screen.findByText("Ravi Kumar")).closest("tr")!;
    expect(within(row).getByText("Finance")).toBeInTheDocument();
    expect(within(row).getByLabelText("Role for Ravi Kumar")).toHaveValue("employee");
  });

  it("checks the password length before calling the API and shows a duplicate-email error", async () => {
    const calls = mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Users");
    await screen.findByText("Alice");
    await user.click(screen.getByRole("button", { name: "Add employee" }));
    const form = screen.getByRole("form", { name: "Add employee" });
    await user.type(within(form).getByLabelText("Work email"), "alice@example.com");
    const password = within(form).getByLabelText("Initial password");
    await user.clear(password);
    await user.type(password, "short");
    await user.click(within(form).getByRole("button", { name: "Add employee" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("at least 12 characters");
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/admin/users")).toBe(false);

    await user.type(password, "-long-enough-now");
    await user.click(within(form).getByRole("button", { name: "Add employee" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(screen.getByRole("form", { name: "Add employee" })).toBeInTheDocument(); // stays open to fix the email
  });

  it("deletes a user after confirmation, never offers deleting yourself", async () => {
    const backend = adminBackend();
    const calls = mockAdmin(backend);
    render(<App />);
    const user = await openAdmin("Users");
    const row = (await screen.findByText("Alice")).closest("tr")!;
    const myRow = within(screen.getByRole("table")).getByText("Dev User").closest("tr")!;
    expect(within(myRow).queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Delete Alice" }));
    await answerDialog(user, /Delete Alice\?/, "Cancel");
    expect(calls.some((c) => c.method === "DELETE" && c.path.startsWith("/api/v1/admin/users/"))).toBe(false);

    await user.click(within(row).getByRole("button", { name: "Delete Alice" }));
    await answerDialog(user, /Delete Alice\?/, "Delete user");
    expect(await screen.findByText("Deleted Alice.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "DELETE" && c.path === "/api/v1/admin/users/2")).toBeDefined();
    await waitFor(() => expect(screen.queryByText("Alice")).not.toBeInTheDocument());
    expect(backend.state.users.map((u) => u.id)).toEqual([1]);
  });

  it("shows the last-admin refusal from the server", async () => {
    mockAdmin(adminBackend());
    render(<App />);
    const user = await openAdmin("Users");
    const row = within(await screen.findByRole("table")).getByText("Dev User").closest("tr")!;
    await user.selectOptions(within(row).getByLabelText("Role for Dev User"), "employee");
    await answerDialog(user, /Remove your own admin access\?/, "Remove my admin access");
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
    // Cancelling the warning changes nothing.
    await user.selectOptions(within(row).getByLabelText("Role for Dev User"), "employee");
    await answerDialog(user, /Remove your own admin access\?/, "Cancel");
    expect(calls.some((c) => c.path === "/api/v1/admin/users/1/role")).toBe(false);
    expect(within(row).getByLabelText("Role for Dev User")).toHaveValue("admin");

    await user.selectOptions(within(row).getByLabelText("Role for Dev User"), "employee");
    await answerDialog(user, /Remove your own admin access\?/, "Remove my admin access");
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
    const user = await openAdmin("Document Access");
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
    const user = await openAdmin("Document Access");
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
    await fillNewDepartment(user, "Ops"); // slug suggested: "ops"
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
