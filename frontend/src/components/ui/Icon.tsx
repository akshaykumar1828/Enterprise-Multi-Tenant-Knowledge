// A few inline stroke icons (no icon library). Decorative unless a label is given.

const PATHS = {
  chat: "M4 5h16v11H8l-4 4V5z",
  documents: "M7 3h7l5 5v13H7V3zm7 0v5h5",
  admin: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z",
  send: "M4 12l16-8-6 16-2-7-8-1z",
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zm5 12l4 4",
  chevron: "M9 6l6 6-6 6",
  upload: "M12 16V4m0 0l-5 5m5-5l5 5M5 20h14",
  plus: "M12 5v14M5 12h14",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z",
  alert: "M12 4l9 16H3L12 4zm0 6v4m0 3v.5",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, label, size = 18 }: { name: IconName; label?: string; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
