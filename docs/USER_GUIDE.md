# User guide

This guide covers using the website, first as an employee and then as an administrator. For how the system works
inside, see [ARCHITECTURE.md](ARCHITECTURE.md); for the exact access rules, see [AUTHORIZATION.md](AUTHORIZATION.md).

- [Logging in](#logging-in)
- [Asking questions (Chat)](#asking-questions-chat)
- [The document library (Documents)](#the-document-library-documents)
- [Your account](#your-account)
- [Administration](#administration)
- [What each person can see](#what-each-person-can-see)
- [Troubleshooting](#troubleshooting)

## Logging in

Log in with the work email and password your administrator gave you. There is no sign-up page: if you don't have an
account yet, ask an administrator to add you.

After 60 minutes (the default) your session ends, and you are asked to log in again.

## Asking questions (Chat)

Type a question in the box at the bottom. Press **Enter** to send it, or **Shift + Enter** for a new line. On a new
chat, you can also click one of the example questions.

**Reading an answer**
- Every statement that comes from a document ends with a citation number, such as `[1]` or `[2]`.
- Click **Show sources** under the answer to see the passages that were used: each one's document name, section or
  page, and the text itself. Click **Hide sources** to fold them away again.
- If your documents don't contain the answer, the assistant says that the information is not available in the
  provided documents. It doesn't guess.

**Sources only.** Tick **Sources only — show matching documents without a written answer** to skip the written
answer and see just the most relevant passages. It's faster, and useful when you want to read the originals.

**Tips for good questions**
- Be specific. "How often must customers be updated during a Sev-1 incident?" works better than "incidents?".
- Use the words your documents use: product names, team names, policy names.
- Ask one thing at a time.

**How long it takes.** An answer usually takes 5–20 seconds. The first question after the server has been idle for a
while can take up to about a minute, while the answer model loads.

## The document library (Documents)

**Documents** lists every document you have access to; Chat answers only from these.
- **Search documents** filters by title, description or type.
- Each row shows a short description, taken word for word from the document's opening text.
- Rows marked **Managed** belong to the company library. Administrators look after them.

**Adding a document**
1. Under **Add a document**, choose a PDF, TXT or Markdown file of up to 10 MB.
2. Upload it. Processing large files can take a moment.
3. As soon as it finishes, Chat can use it to answer questions.

New uploads are visible to everyone in the company. An administrator can then limit a document to certain
departments.

**Deleting a document.** You can delete documents that you uploaded yourself, from the row's actions. This can't be
undone.

## Your account

Open the user menu (your name, in the sidebar):
- **Change password:** enter your current password, then the new one twice. The new password needs at least 12
  characters.
- **Log out.**

The sidebar also shows your role and departments. They decide which documents you can see.

## Administration

The **Admin** section appears only for administrators. It has three tabs.

### Departments

Departments are the groups that documents can be shared with, for example Engineering, Finance and HR.
- **Create department:** give it a name, and a slug (lowercase letters, digits and `-`). The slug is a stable
  identifier and doesn't change.
- **Rename** a department at any time. Memberships and document access stay as they are.
- **Delete department:** its members lose the access that came only through this department, and documents shared
  only with it become admin-only.

### Users

- **+ Add employee:** enter a name, email and initial password (a strong one is suggested; **Generate** makes a new
  one), and tick their departments. Give the password to the employee privately, and ask them to change it after
  their first login.
- **Role:** switch someone between **Employee** and **Admin**. If you remove your own admin access, you lose the
  Admin section immediately, and another admin has to restore it.
- **Departments:** use **Add to department…** to add someone to a department, or remove them from one. The change
  applies to their very next question.
- **Delete user:** removes the account and its department memberships. Documents they uploaded stay. You can't
  delete yourself or the last remaining admin.

### Document Access

Find a document, open it, and choose **Who can read this document?**
- **Everyone in the company**, or
- one or more **departments**. With no departments selected, only administrators can read it.

Click **Save access**. The change applies to the very next search: nobody needs to log in again.

## What each person can see

| Document is shared with | Employee in that department | Other employee | Admin |
|---|---|---|---|
| Everyone in the company | ✔ | ✔ | ✔ |
| Selected departments | ✔ | ✘ | ✔ |
| No departments (administrators only) | ✘ | ✘ | ✔ |

These rules apply everywhere: in Chat answers, in the sources under an answer, and in the Documents list. A document
someone can't read is never searched for their question. If you ask about something only another department can
read, the assistant answers as if the information were not available; it doesn't reveal that the document exists.

## Troubleshooting

| What you see | What to do |
|---|---|
| "Your session has expired. Please log in again." | Log in again. Sessions last 60 minutes by default. |
| "The local AI model (Ollama) is not reachable…" | The answer model isn't running on the server. Ask an administrator to start Ollama. Meanwhile, **Sources only** still works. |
| The answer says the information is not available | The documents you can access don't cover it. Try other wording, or ask an administrator whether the document exists and whether you should have access. |
| An expected document is missing from Documents | It's shared with departments you don't belong to. Ask an administrator. |
| "We couldn't reach the server…" | Check your connection. If it persists, the server may be down. |
| Upload refused | Only PDF, TXT and Markdown files up to 10 MB are accepted. |
