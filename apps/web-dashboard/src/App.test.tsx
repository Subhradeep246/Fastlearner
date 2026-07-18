import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("dashboard App", () => {
  it("renders the shared application shell", () => {
    expect(renderToStaticMarkup(<App />)).toContain("FastLearner dashboard");
  });
});
