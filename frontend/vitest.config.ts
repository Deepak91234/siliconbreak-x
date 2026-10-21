import { defineConfig } from "vitest/config";

// Unit tests cover the pure logic (event reducer, wire formats, diff/waveform
// parsing). They run in Node and never talk to a backend.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
