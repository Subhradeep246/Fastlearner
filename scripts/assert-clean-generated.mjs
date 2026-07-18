import { spawnSync } from "node:child_process";

const paths = [
  "packages/contracts/openapi.json",
  "packages/contracts/src/generated",
];
const result = spawnSync("git", ["diff", "--exit-code", "--", ...paths], {
  stdio: "inherit",
  shell: process.platform === "win32",
});

if (result.error) throw result.error;
if (result.status !== 0) {
  console.error(
    "Generated contracts are stale. Run `npm run contracts:generate`.",
  );
  process.exit(result.status ?? 1);
}
