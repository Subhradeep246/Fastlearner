import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AppShell } from "./index";

describe("AppShell", () => {
  it("renders its accessible heading", () => {
    expect(renderToStaticMarkup(<AppShell title="Study" />)).toContain(
      "<h1>Study</h1>",
    );
  });
});
