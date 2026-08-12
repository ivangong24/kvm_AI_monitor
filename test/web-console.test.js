import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const html = await readFile(new URL("../kvm-agent/index.html", import.meta.url), "utf8");

test("AI Usage web console inline JavaScript parses", () => {
  const start = html.lastIndexOf("<script>") + "<script>".length;
  const end = html.indexOf("</script>", start);
  assert.ok(start >= "<script>".length && end > start, "expected an inline application script");
  assert.doesNotThrow(() => new Function(html.slice(start, end)));
});

test("AI Usage contribution heatmap exposes accessible drill-down controls", () => {
  assert.match(html, /id="usage-heatmap"[^>]+role="group"/);
  assert.match(html, /id="usage-day-detail"[^>]+aria-live="polite"/);
  assert.match(html, /View accessible daily data table/);
  assert.match(html, /class="heatmap-cell level-\$\{level\}"[^>]+aria-pressed="false"/);
  assert.match(html, /button\.addEventListener\("click"/);
  assert.match(html, /prefers-reduced-motion: reduce/);
});
