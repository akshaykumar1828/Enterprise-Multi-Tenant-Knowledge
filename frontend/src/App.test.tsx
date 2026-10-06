import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";
import { EXAMPLE_QUESTIONS } from "./components/ChatView";
import { ANSWER, TOKEN, USER, mockApi } from "./test/mockApi";

const STORAGE_KEY = "ek.accessToken";

async function logIn(user = userEvent.setup()) {
  await user.type(screen.getByLabelText("Email"), "dev@example.com");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("button", { name: /^Account:/ });
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
    expect(await screen.findByRole("button", { name: "Account: Dev User" })).toBeInTheDocument();
  });
});

describe("one company: no self-service sign-up", () => {
  it("the login page offers no way to create an account or organization", async () => {
    const calls = mockApi();
    render(<App />);
    expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(screen.getByText(/Ask your administrator to add you/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Create an account|Create organization/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/organization name/i)).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("shows the company name after login", async () => {
    mockApi();
    render(<App />);
    await logIn();
    expect(screen.getByText(USER.tenant.name)).toBeInTheDocument();
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

    // Inline [1] is a button; the list shows every returned source with a readable title.
    const citation = screen.getByRole("button", { name: "Show source 1" });
    expect(citation).toHaveTextContent("[1]");
    await user.click(screen.getByRole("button", { name: /Show sources/ }));
    expect(screen.getByRole("button", { name: /Source 2: Sample it security policy.*page 3/ })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Source 1 details/ })).not.toBeInTheDocument();

    // Selecting the citation reveals that source's details (from retrieval metadata).
    await user.click(citation);
    const details = screen.getByRole("region", { name: "Source 1 details" });
    expect(within(details).getByText("sample_company_handbook.md#2")).toBeInTheDocument();
    expect(within(details).getByText("Annual Leave")).toBeInTheDocument();
    expect(within(details).getByText(/20 days of annual paid leave/)).toBeInTheDocument();
    expect(citation).toHaveAttribute("aria-pressed", "true");

    // Selecting source 2 in the list shows its PDF page; clicking again hides it.
    const second = screen.getByRole("button", { name: /Source 2: Sample it security policy/ });
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

    await user.click(screen.getByRole("button", { name: "Account: Dev User" }));
    await user.click(screen.getByRole("button", { name: "Log out" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(USER.tenant.name)).not.toBeInTheDocument();
  });
});

describe("change password", () => {
  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: "Account: Dev User" }));
    await user.click(screen.getByRole("button", { name: "Change password" }));
    return screen.getByRole("dialog", { name: "Change password" });
  }

  it("changes the signed-in user's own password", async () => {
    const calls = mockApi({ "POST /api/v1/auth/change-password": () => ({ status: 204, body: null }) });
    render(<App />);
    const user = await logIn();
    const dialog = await openDialog(user);
    expect(within(dialog).getByLabelText("Current password")).toHaveFocus();

    await user.type(within(dialog).getByLabelText("Current password"), "correct horse battery");
    await user.type(within(dialog).getByLabelText("New password"), "a-brand-new-password");
    await user.type(within(dialog).getByLabelText("Repeat new password"), "a-brand-new-password");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));

    expect(await within(dialog).findByText(/Your password has been changed/)).toBeInTheDocument();
    const call = calls.find((c) => c.path === "/api/v1/auth/change-password")!;
    expect(call.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(call.body).toEqual({ current_password: "correct horse battery", new_password: "a-brand-new-password" });
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Account:/ })).toBeInTheDocument(); // still signed in
  });

  it("checks the new password in the browser and shows a wrong current password from the server", async () => {
    const calls = mockApi({
      "POST /api/v1/auth/change-password": () => ({
        status: 400, body: { error: { code: "wrong_current_password", message: "Your current password is incorrect." } },
      }),
    });
    render(<App />);
    const user = await logIn();
    const dialog = await openDialog(user);
    await user.type(within(dialog).getByLabelText("Current password"), "wrong-password");
    await user.type(within(dialog).getByLabelText("New password"), "a-brand-new-password");
    await user.type(within(dialog).getByLabelText("Repeat new password"), "a-different-password");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("do not match");
    expect(calls.some((c) => c.path === "/api/v1/auth/change-password")).toBe(false);

    await user.clear(within(dialog).getByLabelText("Repeat new password"));
    await user.type(within(dialog).getByLabelText("Repeat new password"), "a-brand-new-password");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("current password is incorrect");
    expect(screen.getByRole("button", { name: /^Account:/ })).toBeInTheDocument(); // not logged out

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("first-time experience", () => {
  it("explains the product and asks an example question with one click", async () => {
    const calls = mockApi();
    render(<App />);
    const user = await logIn();

    expect(screen.getByRole("heading", { name: "Hi Dev, what would you like to know?" })).toBeInTheDocument();
    expect(screen.getByText(/every answer lists the sources it used/)).toBeInTheDocument();
    const examples = within(screen.getByRole("list", { name: "Try one of these" })).getAllByRole("button");
    expect(examples.length).toBeGreaterThanOrEqual(3);

    await user.click(screen.getByRole("button", { name: EXAMPLE_QUESTIONS[0] }));
    expect(await screen.findByText(/Employees receive 20 days/)).toBeInTheDocument(); // the mock answers any question
    expect(calls.find((c) => c.path === "/api/v1/query")!.body).toEqual({ question: EXAMPLE_QUESTIONS[0], retrieve_only: false });
    // The welcome makes way for the conversation, which can be started over.
    expect(screen.queryByRole("heading", { name: /what would you like to know/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.getByRole("heading", { name: /what would you like to know/ })).toBeInTheDocument();
  });

  it("shows who is signed in, with role and departments, in the account menu", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => ({ status: 200, body: { ...USER, departments: [{ id: 3, slug: "finance", name: "Finance" }] } }),
    });
    render(<App />);
    const user = await logIn();
    expect(screen.getByText("Employee · Finance")).toBeInTheDocument();

    const button = screen.getByRole("button", { name: "Account: Dev User" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    const panel = screen.getByRole("region", { name: "Your account" });
    expect(within(panel).getByText("dev@example.com")).toBeInTheDocument();
    expect(within(panel).getByText(USER.tenant.name)).toBeInTheDocument();
    expect(within(panel).getByText("Finance")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: "Your account" })).not.toBeInTheDocument();
  });

});

describe("source panels", () => {
  const sourceCards = () => screen.getAllByRole("button", { name: /^Source \d+:/ });
  const openDetails = () => screen.queryAllByRole("region", { name: /^Source \d+ details$/ });

  it("1. the source list is collapsed when an answer loads, and every source is closed (even cited ones)", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);
    await screen.findByText(/Employees receive 20 days/);
    const toggle = screen.getByRole("button", { name: "Show sources 2" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryAllByRole("button", { name: /^Source \d+:/ })).toHaveLength(0); // list hidden
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Hide sources 2" })).toHaveAttribute("aria-expanded", "true");
    expect(sourceCards()).toHaveLength(2);
    expect(sourceCards().map((card) => card.getAttribute("aria-expanded"))).toEqual(["false", "false"]);
    expect(screen.getByRole("button", { name: /Source 1: .*cited/ })).toHaveAttribute("aria-expanded", "false");
    expect(openDetails()).toHaveLength(0);
    expect(screen.queryByText(/20 days of annual paid leave per calendar year\./)).not.toBeInTheDocument(); // passage hidden
    await user.click(screen.getByRole("button", { name: "Hide sources 2" }));
    expect(screen.queryAllByRole("button", { name: /^Source \d+:/ })).toHaveLength(0);
  });

  it("clicking a [n] citation reveals the list and opens that source", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);
    await user.click(await screen.findByRole("button", { name: "Show source 1" }));
    expect(screen.getByRole("button", { name: /Hide sources/ })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Source 1 details" })).toBeInTheDocument();
  });

  it("in sources-only mode the list is shown straight away (cards still closed)", async () => {
    mockApi({ "POST /api/v1/query": () => ({ status: 200, body: { ...ANSWER, answer: null, citations: [], llm_model: null } }) });
    render(<App />);
    const user = await logIn();
    await user.click(screen.getByLabelText(/Sources only/));
    await ask(user);
    await screen.findByText(/no AI answer was generated/);
    expect(sourceCards()).toHaveLength(2);
    expect(sourceCards().every((card) => card.getAttribute("aria-expanded") === "false")).toBe(true);
  });

  it("2./3. clicking a source opens it with passage, section, file and cited status; it can be closed again", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);
    await user.click(await screen.findByRole("button", { name: /Show sources/ }));
    const card = screen.getByRole("button", { name: /Source 1: Sample company handbook/ });

    await user.click(card);
    expect(card).toHaveAttribute("aria-expanded", "true");
    const details = screen.getByRole("region", { name: "Source 1 details" });
    expect(within(details).getByText(/20 days of annual paid leave/)).toBeInTheDocument();
    expect(within(details).getByText("Section").nextSibling).toHaveTextContent("Annual Leave");
    expect(within(details).getByText("File").nextSibling).toHaveTextContent("sample_company_handbook.md");
    expect(within(details).getByText("Cited in the answer").nextSibling).toHaveTextContent("Yes");

    // Close with the Close button: the card collapses and keeps focus.
    await user.click(within(details).getByRole("button", { name: "Close source 1" }));
    expect(card).toHaveAttribute("aria-expanded", "false");
    expect(openDetails()).toHaveLength(0);
    expect(card).toHaveFocus();

    // Close by clicking the card again.
    await user.click(card);
    expect(openDetails()).toHaveLength(1);
    await user.click(card);
    expect(openDetails()).toHaveLength(0);
  });

  it("4. technical details stay closed until explicitly opened", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);
    await user.click(await screen.findByRole("button", { name: /Show sources/ }));
    await user.click(screen.getByRole("button", { name: /Source 2: Sample it security policy/ }));
    const details = screen.getByRole("region", { name: "Source 2 details" });
    const technical = within(details).getByText("Technical details").closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    await user.click(within(details).getByText("Technical details"));
    expect(technical).toHaveAttribute("open");
    // Closing and reopening the source starts the technical details closed again.
    await user.click(within(details).getByRole("button", { name: "Close source 2" }));
    await user.click(screen.getByRole("button", { name: /Source 2: Sample it security policy/ }));
    const reopened = screen.getByRole("region", { name: "Source 2 details" });
    expect(within(reopened).getByText("Technical details").closest("details")).not.toHaveAttribute("open");
  });

  it("5. asking another question collapses every source list and closes every source", async () => {
    mockApi();
    render(<App />);
    const user = await logIn();
    await ask(user);
    await user.click(await screen.findByRole("button", { name: /Show sources/ }));
    await user.click(screen.getByRole("button", { name: /Source 1: Sample company handbook/ }));
    expect(openDetails()).toHaveLength(1);

    await ask(user, "How long are customer records retained?");
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Show sources/ })).toHaveLength(2)); // both answers
    expect(screen.queryAllByRole("button", { name: /^Hide sources/ })).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: /^Source \d+:/ })).toHaveLength(0);
    expect(openDetails()).toHaveLength(0);
  });
});
