import { describe, expect, it } from "vitest";
import { contentManifest } from "./index";

describe("content manifest", () => {
  it("starts empty until reviewed curriculum packs are added", () => {
    expect(contentManifest).toEqual({ schemaVersion: 1, packs: [] });
  });
});
