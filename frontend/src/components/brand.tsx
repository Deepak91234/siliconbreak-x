/** SiliconBreak X mark: a chip outline crossed by the "X", in the brand gradient. */

import { useId } from "react";

export function BrandMark({ size = 32 }: { size?: number }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg viewBox="0 0 40 40" width={size} height={size} aria-hidden>
      <defs>
        <linearGradient id={`g${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="rgb(var(--brand-a))" />
          <stop offset="0.6" stopColor="rgb(var(--brand-b))" />
          <stop offset="1" stopColor="rgb(var(--brand-c))" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="38" height="38" rx="10" fill={`url(#g${id})`} />
      <rect x="10" y="10" width="20" height="20" rx="4" fill="none" stroke="white" strokeOpacity="0.45" strokeWidth="1.6" />
      <path d="M14 6v4M20 6v4M26 6v4M14 30v4M20 30v4M26 30v4M6 14h4M6 20h4M6 26h4M30 14h4M30 20h4M30 26h4"
            stroke="white" strokeOpacity="0.5" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M14.5 14.5l11 11M25.5 14.5l-11 11" stroke="white" strokeWidth="3.4" strokeLinecap="round" />
    </svg>
  );
}

export function BrandName({ className = "" }: { className?: string }) {
  return (
    <span className={`font-semibold tracking-tight ${className}`}>
      SiliconBreak <span className="sbx-gradient-text">X</span>
    </span>
  );
}
