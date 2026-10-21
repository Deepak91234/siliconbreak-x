import type { Config } from "tailwindcss";

// Colours are CSS variables (src/index.css) so the dark default and the light
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
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "SFMono-Regular", "ui-monospace", "Menlo", "monospace"],
      },
      keyframes: {
        pulseDot: { "0%,100%": { opacity: "1" }, "50%": { opacity: ".35" } },
      },
      animation: { "pulse-dot": "pulseDot 1.4s ease-in-out infinite" },
    },
  },
  plugins: [],
} satisfies Config;
