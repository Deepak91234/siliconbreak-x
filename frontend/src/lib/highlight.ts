/**
 * Syntax highlighting with highlight.js core + only the languages we show
 * (Verilog for RTL, Tcl for XDC constraints, JSON for specs), keeping the
 * bundle small. hljs escapes the source text, so its HTML output is safe to
 * insert; nothing else is ever passed to dangerouslySetInnerHTML.
 */

import hljs from "highlight.js/lib/core";
import verilog from "highlight.js/lib/languages/verilog";
import tcl from "highlight.js/lib/languages/tcl";
import json from "highlight.js/lib/languages/json";

hljs.registerLanguage("verilog", verilog);
hljs.registerLanguage("tcl", tcl);
hljs.registerLanguage("json", json);

export type CodeLanguage = "verilog" | "tcl" | "json" | "plaintext";

export function languageFor(fileName: string): CodeLanguage {
  const n = fileName.toLowerCase();
  if (n.endsWith(".v") || n.endsWith(".sv") || n.endsWith(".vh")) return "verilog";
  if (n.endsWith(".xdc") || n.endsWith(".tcl")) return "tcl";
  if (n.endsWith(".json")) return "json";
  return "plaintext";
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlight(code: string, lang: CodeLanguage): string {
  if (lang === "plaintext") return escapeHtml(code);
  try {
    return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
  } catch {
    return escapeHtml(code);
  }
}

/**
 * Highlight a whole file and split the HTML into per-line fragments, closing and
 * reopening spans across newlines so multi-line comments keep their colour.
 */
export function highlightLines(code: string, lang: CodeLanguage): string[] {
  const html = highlight(code, lang);
  const out: string[] = [];
  const open: string[] = [];
  let cur = "";
  const re = /(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+|<)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    if (m[1]) {
      open.push(m[1]);
      cur += m[1];
    } else if (m[2]) {
      open.pop();
      cur += m[2];
    } else if (m[3]) {
      cur += "</span>".repeat(open.length);
      out.push(cur);
      cur = open.join("");
    } else {
      cur += m[4];
    }
  }
  cur += "</span>".repeat(open.length);
  out.push(cur);
  return out;
}
