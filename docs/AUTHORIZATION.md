# Authentication and authorization

This page covers who can sign in, who can read which document, and the authorization state currently in production.
It reflects the rollout completed on **2026-10-01** (git checkpoint `1992fbf`).

## Authentication

- **Passwords** are hashed with Argon2id; only the hash is stored. Passwords must be 12–256 characters long.
- **Access tokens** are HS256 JWTs signed with `JWT_SECRET_KEY`, and last `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60,
  allowed range 5–1440). Validation pins the algorithm and requires `exp`, `iat` and `sub`, so `alg: none` and
  algorithm-confusion tokens are rejected.
- **No permissions in the token.** The token identifies the user and nothing else. Role and departments are re-read
  from the database on every request (`load_access_scope`), so access changes take effect on the user's next request.
- **Registration.** `REGISTRATION_MODE` defaults to `closed` in production. Public registration only ever creates a
  *new* organization (tenant) whose first user is an *employee*. Nothing can make a user an admin except the
  operator CLI:
  `python -m src.rag.users set-role --email <email> --role admin`
- **Login rate limit:** 5 attempts per minute per client IP and email (production).

## Authorization model

Everything is scoped to the caller's tenant. Within it:

| Role | Can read |
|---|---|
| `admin` | every document of the tenant |
| `employee` | documents with `visibility = 'company'`, plus documents with `visibility = 'departments'` that are linked to at least one of the employee's departments |

The rule is implemented once, as `DOCUMENT_ACCESS_FILTER` in `src/rag/access.py`. It is used for retrieval, the
document list, uploads (duplicate check) and deletes. It runs inside SQL, before any ranking or `LIMIT`.

**Fail-closed details:**
- Only the literal value `company` opens a document to everyone. Any other visibility needs a matching department
  link.
- A `departments` document with **no** department links is readable by **admins only**.
- A tenant whose authorized corpus is empty behaves exactly like an empty tenant, so a response never reveals that
  restricted documents exist.

## Departments (tenant `default`)

| ID | Slug | Name | Documents linked |
|---|---|---|---|
| 396 | `engineering` | Engineering | 682 |
| 397 | `finance` | Finance | 39 |
| 398 | `hr` | HR | 8 |
| 399 | `marketing` | Marketing | 16 |
| 400 | `sales-customer-success` | Sales/Customer Success | 452 |
| 401 | `product-design` | Product/Design | 117 |
| 402 | `legal-compliance` | Legal/Compliance | 57 |
| 403 | `executive-leadership` | Executive Leadership | 2 |

A document with several departments counts once in each, which is why the column adds up to 1,373 links rather than
1,166 documents. There are currently **no** user memberships; the admin assigns them as employees are onboarded.

**Policy decisions (signed off on 2026-10-01):**
- **Security belongs to Engineering.**
- **Executive Leadership** is a department for executive and leadership-only material, such as exec staff briefings
  and headcount planning.
- **`admin_only` is not a department.** It is reserved for genuinely restricted individual, personal or sensitive
  material: individual compensation, interview assessments, accommodation details, and 1:1 or performance notes about
  named people.

## Document visibility in production

| Class | Documents | Stored as | Who can read |
|---|---|---|---|
| Company-wide | 31 | `visibility = company`, no links | everyone in the tenant |
| Department | 1,166 | `visibility = departments` + 1..n links | admins, and members of any linked department |
| Admin-only | 9 | `visibility = departments`, **0 links** | admins only |
| Pending review | 16 | `visibility = departments`, **0 links** | admins only |
| **Total** | **1,222** | 1,191 `departments` + 31 `company`; 1,373 links | |

The two sample documents (a synthetic "Brightfield Analytics" handbook and IT security policy, ids 1 and 2, both
company-wide with no department links) were removed on 2026-10-03, after a verified backup
(`enterprise_rag-pre-sample-removal-20261003-004011.dump`). They are now test fixtures in `tests/fixtures/`.
The numbers in the rollout sections below are as of the rollout (1,224 documents, 33 company-wide).

The 31 company-wide documents are 14 messages from broad Slack channels
(all-hands, announcements, general, social), 16 Confluence pages that state they apply to everyone, and 1 company
town-hall transcript.

### Admin-only (9)

These are individual candidate and employee records: interview transcripts and debriefs, offer and compensation
details, accommodation requests, manager 1:1 notes, and a contractor onboarding ticket. Their document ids are
**208, 255, 274, 282, 741, 830, 864, 869 and 1003**. The ids are given instead of file names because the file names
contain people's names. Keep these documents admin-only unless the content itself is redacted.

### Pending review (16)

These documents had no evidence that would let reviewers assign a department: customer calls where the Redwood
participants' roles aren't stated, personal scratchpads, and similar. They are **not** labelled `admin_only` in the
policy. They're held at the same effective state (no department links, so admins only) until a person decides.

Document ids: **76, 252, 260, 277, 645, 729, 738, 740, 843, 877, 883, 886, 903, 909, 919, 930.**

To release one, an admin sets its departments with `PUT /api/v1/admin/documents/{id}/access` or in the admin panel's
document-access screen. Don't invent a department: assign one only when the document's content supports it.

## How the classification was made

1. **Dry run** over all 1,224 documents using structured signals only: Slack channel, Confluence owner and sign-off
   teams, Jira and Linear key prefixes, GitHub, HubSpot, Gmail headers, and meeting attendee roles. Overlapping topic
   keywords were never treated as sufficient evidence on their own.
2. **Human review** of every flagged document, done in groups:
   - **13 sensitive or facilities documents**, with a signed sign-off sheet.
   - **233 undetermined documents.** These were reviewed against their content, and every piece of evidence quoted
     was checked word for word against the source.
   - **57 of those** were then reviewed again in detail.
   - **The rest** kept their dry-run classification.
3. **Policy final draft** applying the decisions above (Security → Engineering, Executive Leadership).
4. **Reconciled plan** for all 1,224 documents. Every document appeared exactly once, every department was valid, and
   the counts were exactly 33 / 1,166 / 16 / 9.

## How the rollout was applied (2026-10-01)

1. **Backups:** a verified backup before any change (`enterprise_rag-pre-authz-rollout-20261001-181157.dump`), and a
   second verified backup after the departments were created (`enterprise_rag-pre-authz-access-20261001-181756.dump`).
   See [OPERATIONS.md](OPERATIONS.md#restore-points).
2. **Departments:** the 8 were created with `POST /api/v1/admin/departments`.
3. **Access:** set with `PUT /api/v1/admin/documents/{id}/access`, through the real Admin API with no direct SQL.
   1,191 documents were updated; the 33 company documents were already correct and weren't rewritten. The original
   access of all 1,224 documents was snapshotted first, and an automatic rollback through the same API was ready but
   not needed.
4. **Verification:** all 1,224 documents matched the plan, checked through the Admin API and again directly in the
   database. Only the `visibility` column of `documents` changed, and chunks and embeddings were untouched.
5. **Tests:** the full backend suite and production smoke tests passed; see [OPERATIONS.md](OPERATIONS.md#tests).

## Changing access (admins)

| Task | Admin API | Frontend |
|---|---|---|
| List or create departments | `GET`, `POST /api/v1/admin/departments` | Admin → Departments |
| Rename or delete a department | `PATCH`, `DELETE /api/v1/admin/departments/{id}` (refused while documents are assigned) | Admin → Departments |
| Add an employee (own company only, always an employee, optional departments) | `POST /api/v1/admin/users` with body `{"email", "password", "display_name"?, "department_ids": [...]}` | Admin → Users → Add employee |
| Delete a user (not yourself, not the last admin; memberships removed, uploaded documents kept) | `DELETE /api/v1/admin/users/{id}` | Admin → Users → Delete |
| Make a user admin or employee | `PUT /api/v1/admin/users/{id}/role` (refused if it would leave the company without an admin) | Admin → Users |
| Add or remove a membership | `PUT`, `DELETE /api/v1/admin/users/{id}/departments/{department_id}` | Admin → Users |
| See or set a document's access | `GET`, `PUT /api/v1/admin/documents/{id}/access` with body `{"visibility": "company"\|"departments", "department_ids": [...]}` | Admin → Document access |

Every admin route requires an admin scope loaded from the database. IDs from another tenant return the same 404 as
unknown IDs.

Admins add and delete employees in **Admin → Users** (added 2026-10-03). Before that, the temporary users created
for the rollout smoke tests were removed with a guarded owner transaction (exact IDs, pattern-checked e-mails, row
count verified). Every user changes their own password in the account menu (**Change password**, which requires the
current password; `POST /api/v1/auth/change-password`, rate limited like login). If someone forgets their password,
an operator resets it with `python -m src.rag.users set-password --email <email>`.
