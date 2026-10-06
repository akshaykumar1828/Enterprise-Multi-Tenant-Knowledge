// Presentation helpers for document names and types. Display only: they never change what is
// requested from or sent to the API.

const BENCHMARK_PREFIX = /^dsid_[0-9a-f]{32}__/i;
const EXTENSION = /\.(md|markdown|txt|pdf)$/i;

/** A readable title from a file name: "dsid_…__q3-pricing_review.txt" -> "Q3 pricing review". */
export function documentTitle(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  const words = base.replace(BENCHMARK_PREFIX, "").replace(EXTENSION, "").replace(/[_-]+/g, " ").trim();
  if (!words) return base;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const TYPE_LABELS: Record<string, string> = {
  confluence: "Wiki page",
  slack: "Slack conversation",
  gmail: "Email thread",
  jira: "Jira ticket",
  linear: "Linear issue",
  github: "Pull request",
  google_drive: "Drive document",
  fireflies: "Meeting notes",
  hubspot: "CRM record",
  upload: "Uploaded file",
};

/** A plain-language label for a source type ("google_drive" -> "Drive document"). */
export function documentTypeLabel(sourceType: string, filename = ""): string {
  if (TYPE_LABELS[sourceType]) return TYPE_LABELS[sourceType];
  if (/\.pdf$/i.test(filename)) return "PDF document";
  return "Document";
}

export const DESCRIPTION_MAX_CHARS = 240;

/** A document description for display: whitespace tidied, never longer than DESCRIPTION_MAX_CHARS. */
export function displayDescription(description: string | null | undefined): string | null {
  const text = (description ?? "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  if (text.length <= DESCRIPTION_MAX_CHARS) return text;
  const cut = text.slice(0, DESCRIPTION_MAX_CHARS - 1);
  return `${cut.slice(0, cut.lastIndexOf(" ") > 0 ? cut.lastIndexOf(" ") : cut.length).replace(/[\s,;:]+$/, "")}…`;
}

export function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Two initials for an avatar. */
export function initials(name: string): string {
  const parts = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}
