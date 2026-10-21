/** One glyph per stage status (docs/api.md vocabulary), coloured by tone. */

import { stageStatus, TONE_TEXT } from "../lib/status";
import { cx } from "./ui";

export function StatusIcon({ status, className = "h-4 w-4" }: { status: string; className?: string }) {
  const { tone } = stageStatus(status);
  const common = { viewBox: "0 0 20 20", className: cx(className, TONE_TEXT[tone], "shrink-0"), "aria-label": status, role: "img" } as const;
  switch (status) {
    case "running":
      return (
        <svg {...common} className={cx(common.className, "animate-spin")} fill="none">
          <circle cx="10" cy="10" r="7" stroke="currentColor" strokeOpacity=".25" strokeWidth="2.2" />
          <path d="M17 10a7 7 0 0 0-7-7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
        </svg>
      );
    case "passed":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" fill="currentColor" fillOpacity=".15" stroke="currentColor" strokeWidth="1.5" />
          <path d="M6.5 10.2l2.3 2.3 4.7-5" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "failed":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" fill="currentColor" fillOpacity=".15" stroke="currentColor" strokeWidth="1.5" />
          <path d="M7.2 7.2l5.6 5.6M12.8 7.2l-5.6 5.6" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
        </svg>
      );
    case "skipped":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2.5 2.5" />
          <path d="M7 7l3.5 3-3.5 3M10.5 7l3.5 3-3.5 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "blocked":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" fill="currentColor" fillOpacity=".15" stroke="currentColor" strokeWidth="1.5" />
          <path d="M8 6.5v7M12 6.5v7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      );
    case "unavailable":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.5" />
          <path d="M4.6 15.4L15.4 4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      );
    case "inconclusive":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" fill="currentColor" fillOpacity=".12" stroke="currentColor" strokeWidth="1.5" />
          <path d="M7.8 7.6a2.3 2.3 0 1 1 3.2 2.1c-.6.3-1 .8-1 1.4v.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="10" cy="14.2" r=".9" fill="currentColor" />
        </svg>
      );
    case "cancelled":
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.5" />
          <rect x="7" y="7" width="6" height="6" rx="1" fill="currentColor" />
        </svg>
      );
    case "pending":
    default:
      return (
        <svg {...common} fill="none">
          <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.5" strokeOpacity=".7" />
        </svg>
      );
  }
}
