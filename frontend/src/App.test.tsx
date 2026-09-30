import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";
import { ANSWER, TOKEN, USER, mockApi } from "./test/mockApi";

const STORAGE_KEY = "ek.accessToken";

async function logIn(user = userEvent.setup()) {
  await user.type(screen.getByLabelText("Email"), "dev@example.com");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("button", { name: "Log out" });
  return user;
}

async function ask(user: ReturnType<typeof userEvent.setup>, question = ANSWER.question) {
  await user.type(screen.getByLabelText("Question"), question);
  await user.click(screen.getByRole("button", { name: "Ask" }));
}

describe("login", () => {
  it("logs in, stores the token, and shows the user's organization", async () => {
    const calls = mockApi();
    render(<App />);
    await logIn();

    expect(screen.getByText(USER.tenant.name)).toBeInTheDocument();
    expect(screen.getByText(USER.display_name!)).toBeInTheDocument();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(TOKEN);
    const login = calls.find((c) => c.path === "/api/v1/auth/login")!;
    expect(login.body).toEqual({ email: "dev@example.com", password: "correct horse battery" });
    expect(calls.find((c) => c.path === "/api/v1/auth/me")!.headers.Authorization).toBe(`Bearer ${TOKEN}`);
  });

  it("shows the server's message for invalid credentials and stays on the login screen", async () => {
    mockApi({
      "POST /api/v1/auth/login": () => ({
        status: 401,
        body: { error: { code: "invalid_credentials", message: "Incorrect email or password." } },
      }),
    });
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "dev@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong password!");
    await user.click(screen.getByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("restores a stored session on reload", async () => {
    window.localStorage.setItem(STORAGE_KEY, TOKEN);
    mockApi();
    render(<App />);
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
  });
});

describe("registration", () => {
  it("registers an organization, then logs in automatically", async () => {
    const calls = mockApi();
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Create an account" }));
    await user.type(screen.getByLabelText("Organization name"), "Acme Corp");
    await user.type(screen.getByLabelText(/Your name/), "Alice");
    await user.type(screen.getByLabelText("Email"), "alice@acme.test");
    await user.type(screen.getByLabelText(/Password/), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    const register = calls.find((c) => c.path.startsWith("/api/v1/auth/register"))!;
    expect(register.body).toEqual({
      organization_name: "Acme Corp", email: "alice@acme.test", password: "a-long-enough-password", display_name: "Alice",
    });
    expect(calls.map((c) => c.path)).toEqual(["/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/me"]);
  });

  it("rejects a short password before calling the API", async () => {
    const calls = mockApi();
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Create an account" }));
    await user.type(screen.getByLabelText("Organization name"), "Acme Corp");
    await user.type(screen.getByLabelText("Email"), "alice@acme.test");
    await user.type(screen.getByLabelText(/Password/), "short");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("at least 12 characters");
    expect(calls).toHaveLength(0);
  });

  it("shows a duplicate-email error from the server", async () => {
    mockApi({
      "POST /api/v1/auth/register": () => ({
        status: 409,
        body: { error: { code: "email_already_registered", message: "An account with this email already exists." } },
      }),
    });
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Create an account" }));
    await user.type(screen.getByLabelText("Organization name"), "Acme Corp");
    await user.type(screen.getByLabelText("Email"), "taken@acme.test");
    await user.type(screen.getByLabelText(/Password/), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});

describe("authenticated query and citations", () => {
  it("sends the bearer token, never a tenant, and renders the answer with citations", async () => {
    const calls = mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);

    expect(await screen.findByText(/Employees receive 20 days/)).toBeInTheDocument();
    const query = calls.find((c) => c.path === "/api/v1/query")!;
    expect(query.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(query.body).toEqual({ question: ANSWER.question, retrieve_only: false });
    expect(JSON.stringify(query.body)).not.toMatch(/tenant/i);

    // Inline [1] is a button; the list shows every returned source.
    const citation = screen.getByRole("button", { name: "Show source 1" });
    expect(citation).toHaveTextContent("[1]");
    expect(screen.getByRole("button", { name: /\[2\] sample_it_security_policy\.pdf · p\. 3/ })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Source 1 details/ })).not.toBeInTheDocument();

    // Selecting the citation reveals that source's details (from retrieval metadata).
    await user.click(citation);
    const details = screen.getByRole("region", { name: "Source 1 details" });
    expect(within(details).getByText("sample_company_handbook.md#2")).toBeInTheDocument();
    expect(within(details).getByText("Annual Leave")).toBeInTheDocument();
    expect(within(details).getByText(/20 days of annual paid leave/)).toBeInTheDocument();
    expect(citation).toHaveAttribute("aria-pressed", "true");

    // Selecting source 2 in the list shows its PDF page; clicking again hides it.
    const second = screen.getByRole("button", { name: /\[2\] sample_it_security_policy\.pdf/ });
    await user.click(second);
    const pdf = screen.getByRole("region", { name: "Source 2 details" });
    expect(within(pdf).getByText("3")).toBeInTheDocument();
    await user.click(second);
    expect(screen.queryByRole("region", { name: /details/ })).not.toBeInTheDocument();
  });

  it("does not link a citation number with no matching source", async () => {
    mockApi({
      "POST /api/v1/query": () => ({ status: 200, body: { ...ANSWER, answer: "Supported [1]. Unmatched [5]." } }),
    });
    render(<App />);
    const user = await logIn();
    await ask(user);
    expect(await screen.findByText(/Unmatched \[5\]/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show source 5" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show source 1" })).toBeInTheDocument();
  });

  it("supports sources-only questions", async () => {
    const calls = mockApi({
      "POST /api/v1/query": () => ({ status: 200, body: { ...ANSWER, answer: null, citations: [], llm_model: null } }),
    });
    render(<App />);
    const user = await logIn();
    await user.click(screen.getByLabelText(/Sources only/));
    await ask(user);
    expect(await screen.findByText(/no AI answer was generated/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/query")!.body).toEqual({ question: ANSWER.question, retrieve_only: true });
  });

  it("shows a loading state while waiting", async () => {
    let release: () => void = () => {};
    mockApi();
    const pending = new Promise<void>((resolve) => (release = resolve));
    const fetchMock = globalThis.fetch as unknown as { getMockImplementation: () => typeof fetch };
    const original = fetchMock.getMockImplementation();
    (globalThis.fetch as unknown as { mockImplementation: (f: typeof fetch) => void }).mockImplementation(
      async (input, init) => {
        if (String(input).endsWith("/api/v1/query")) await pending;
        return original(input, init);
      },
    );
    render(<App />);
    const user = await logIn();
    await ask(user);
    expect(await screen.findByRole("status")).toHaveTextContent("Searching documents");
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
    release();
    expect(await screen.findByText(/Employees receive 20 days/)).toBeInTheDocument();
  });
});

describe("errors", () => {
  it("returns to login with a notice when the API answers 401", async () => {
    mockApi({
      "POST /api/v1/query": () => ({
        status: 401,
        body: { error: { code: "token_expired", message: "The access token has expired; log in again." } },
      }),
    });
    render(<App />);
    const user = await logIn();
    await ask(user);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Your session has expired");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("explains a Gemini quota error and offers a sources-only retry", async () => {
    let attempt = 0;
    const calls = mockApi({
      "POST /api/v1/query": (call) => {
        attempt += 1;
        if ((call.body as { retrieve_only: boolean }).retrieve_only) {
          return { status: 200, body: { ...ANSWER, answer: null, citations: [], llm_model: null } };
        }
        return {
          status: 429,
          body: { error: { code: "llm_quota_exceeded", message: "quota", retry_after_seconds: 32.4 } },
        };
      },
    });
    render(<App />);
    const user = await logIn();
    await ask(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("reached its usage limit");
    expect(alert).toHaveTextContent("about 33 seconds");
    await user.click(within(alert).getByRole("button", { name: "Show sources only" }));
    expect(await screen.findByText(/no AI answer was generated/)).toBeInTheDocument();
    expect(attempt).toBe(2);
    expect(calls.filter((c) => c.path === "/api/v1/query").map((c) => (c.body as { retrieve_only: boolean }).retrieve_only))
      .toEqual([false, true]);
  });

  it("reports an unreachable server", async () => {
    mockApi();
    (globalThis.fetch as unknown as { mockRejectedValueOnce: (e: Error) => void }).mockRejectedValueOnce(new TypeError("fetch failed"));
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "dev@example.com");
    await user.type(screen.getByLabelText("Password"), "whatever-password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server");
  });
});

describe("logout", () => {
  it("removes the token and returns to the login screen", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(TOKEN);

    await user.click(screen.getByRole("button", { name: "Log out" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(USER.tenant.name)).not.toBeInTheDocument();
  });
});
