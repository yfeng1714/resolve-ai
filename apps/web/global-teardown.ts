import { rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export default function globalTeardown() {
  rmSync(path.join(root, ".pytest-db.pid"), { force: true });
  rmSync(path.join(root, ".pytest-db.ready"), { force: true });
}
