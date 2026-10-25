import type { Config } from "tailwindcss";

// Colours are CSS variables (src/index.css) so the light default and the dark
// theme share every class name.
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: v("bg"),
        panel: v("panel"),
        raised: v("raised"),
        sunken: v("sunken"),
        line: v("line"),
        "line-strong": v("line-strong"),
        "accent-fg": v("accent-fg"),
        fg: v("fg"),
        muted: v("muted"),
        faint: v("faint"),
        accent: v("accent"),
        ok: v("ok"),
        bad: v("bad"),
        warn: v("warn"),
        info: v("info"),
        idle: v("idle"),
        violet: v("violet"),
        orange: v("orange"),
        brand: v("brand-a"),
        brand2: v("brand-b"),
        brand3: v("brand-c"),
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "SFMono-Regular", "ui-monospace", "Menlo", "monospace"],
      },
      borderRadius: { sm: "var(--radius-sm)", md: "var(--radius-md)", lg: "var(--radius-lg)" },
      boxShadow: { panel: "var(--shadow-panel)", pop: "var(--shadow-pop)" },
      fontSize: { "2xs": ["0.6875rem", { lineHeight: "1rem" }] },
      transitionTimingFunction: { brand: "var(--ease)" },
      keyframes: {
        pulseDot: { "0%,100%": { opacity: "1" }, "50%": { opacity: ".35" } },
      },
      animation: { "pulse-dot": "pulseDot 1.4s ease-in-out infinite" },
    },
  },
  plugins: [],
} satisfies Config;
