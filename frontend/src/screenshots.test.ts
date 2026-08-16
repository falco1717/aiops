import { describe, expect, it } from "vitest";

import { shotUrl, shotsIn } from "./screenshots";

/** Exactly what `Browser.screenshot` returns, path and all. */
const saved = (n: string, url: string) =>
  `Saved /tmp/aiops-browser-42-9zk/${n} (${url}). Password fields are masked. ` +
  `Read the file to look at it.`;

describe("shotsIn", () => {
  it("finds the screenshot a tool result reports", () => {
    expect(shotsIn("tool_result", saved("screenshot-001.png", "https://example.com/a"))).toEqual([
      { name: "screenshot-001.png", url: "https://example.com/a" },
    ]);
  });

  it("finds several, in the order they were taken", () => {
    const text = [saved("screenshot-002.png", "http://10.0.0.4:8989/"), saved("screenshot-003.png", "http://10.0.0.4:8989/x")].join("\n");
    expect(shotsIn("tool_result", text).map((s) => s.name)).toEqual([
      "screenshot-002.png",
      "screenshot-003.png",
    ]);
  });

  it("does not draw the same one twice", () => {
    const one = saved("screenshot-001.png", "https://example.com/");
    expect(shotsIn("tool_result", `${one}\n${one}`)).toHaveLength(1);
  });

  // A URL with brackets in it is a real address, and reading only as far as the
  // first ')' would cut the sentence — and the mask claim with it — in half.
  it("reads a URL that has brackets in it", () => {
    const url = "https://en.wikipedia.org/wiki/Ping_(networking)";
    expect(shotsIn("tool_result", saved("screenshot-004.png", url))[0]?.url).toBe(url);
  });

  it("ignores a result that does not claim the mask was applied", () => {
    expect(
      shotsIn("tool_result", "Saved /tmp/x/screenshot-001.png (https://e.com). Read it."),
    ).toEqual([]);
  });

  it("ignores a name that is not one AIOps generates", () => {
    expect(
      shotsIn("tool_result", saved("screenshot-1.png", "https://e.com")).length +
        shotsIn("tool_result", saved("../../etc/passwd", "https://e.com")).length +
        shotsIn("tool_result", saved("screenshot-001.png.exe", "https://e.com")).length,
    ).toBe(0);
  });

  // The model writing the sentence is not the browser having taken the picture.
  it("only looks at tool results", () => {
    const text = saved("screenshot-001.png", "https://example.com/");
    for (const kind of ["assistant", "thinking", "tool_use", "user", "result", "system"]) {
      expect(shotsIn(kind, text)).toEqual([]);
    }
  });

  it("says nothing about an empty or missing result", () => {
    expect(shotsIn("tool_result", "")).toEqual([]);
    expect(shotsIn("tool_result", null)).toEqual([]);
    expect(shotsIn("tool_result", undefined)).toEqual([]);
  });

  // /g regexes keep a lastIndex. A module-level one that is reused would start
  // the second call part-way through the string and silently find nothing.
  it("does not carry regex state between calls", () => {
    const text = saved("screenshot-001.png", "https://example.com/");
    expect(shotsIn("tool_result", text)).toEqual(shotsIn("tool_result", text));
  });
});

describe("shotUrl", () => {
  it("addresses the run that took it", () => {
    expect(shotUrl(42, "screenshot-001.png")).toBe("/api/runs/42/screenshots/screenshot-001.png");
  });
});
