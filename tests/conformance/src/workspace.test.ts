import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const readJson = (path: string): unknown =>
  JSON.parse(readFileSync(resolve(root, path), "utf8"));

describe("workspace conformance", () => {
  it("contains each required workspace boundary", () => {
    const paths = [
      "apps/desktop/package.json",
      "apps/desktop/src-tauri/Cargo.toml",
      "apps/web-dashboard/package.json",
      "services/api/pyproject.toml",
      "packages/ui/package.json",
      "packages/content/package.json",
      "packages/contracts/package.json",
      "crates/wake-detector/Cargo.toml",
    ];
    for (const path of paths)
      expect(() => readFileSync(resolve(root, path))).not.toThrow();
  });

  it("commits a versioned health endpoint in the generated OpenAPI document", () => {
    const schema = readJson("packages/contracts/openapi.json") as {
      info: { version: string };
      paths: Record<string, unknown>;
    };
    expect(schema.info.version).toBe("0.1.0");
    expect(schema.paths).toHaveProperty("/v1/health");
  });
});
