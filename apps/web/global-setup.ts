import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const readyPath = path.join(root, ".pytest-db.ready");

export default async function globalSetup() {
  const deadline = Date.now() + 5_000;
  while (!existsSync(readyPath) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!existsSync(readyPath)) {
    throw new Error(
      "resolveai_pytest was not prepared before the browser servers reported healthy",
    );
  }
}
