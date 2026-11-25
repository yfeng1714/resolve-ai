import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Page, test } from "@playwright/test";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "../../..");

function startWorker(command: string): ChildProcess {
  return spawn("bash", ["-lc", command], {
    cwd: root,
    detached: true,
    stdio: "ignore",
  });
}

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();
}

test("shows queued, leased, and blocked recovery before settlement", async ({ page }, testInfo) => {
  const shots = testInfo.outputPath("recovery-states");
  mkdirSync(shots, { recursive: true });
  const dir = mkdtempSync(join(tmpdir(), "resolveai-recovery-"));
  const ready = join(dir, "ready");
  const release = join(dir, "release");
  let held: ChildProcess | undefined;
  let restarted: ChildProcess | undefined;
  try {
    await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
    await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
    await page.getByRole("button", { name: "Investigate" }).click();
    await expect(page.locator(".amount")).toHaveText("$42.00");
    await page.getByRole("button", { name: "Sign out" }).click();

    await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
    await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
    await page.getByRole("button", { name: "Approve refund" }).click();
    await page.getByRole("button", { name: "Execute refund" }).click();
    const recovery = page.getByTestId("recovery-state");
    await expect(recovery).toContainText("queued");
    await recovery.scrollIntoViewIfNeeded();
    await page.screenshot({ path: join(shots, "queued.png"), fullPage: true });

    held = startWorker(
      `eval "$(PYTHONPATH=src .venv/bin/python -m resolveai.testing env)" && ` +
        `RESOLVEAI_RECOVERY_HOLD=before_post RESOLVEAI_RECOVERY_READY='${ready}' ` +
        `RESOLVEAI_RECOVERY_RELEASE='${release}' PYTHONPATH=src ` +
        `.venv/bin/python -m resolveai.worker`,
    );
    for (let attempt = 0; attempt < 80 && !existsSync(ready); attempt += 1) {
      if (held.exitCode !== null) {
        throw new Error(`worker exited before the post gate (${held.exitCode})`);
      }
      await page.waitForTimeout(100);
    }
    if (!existsSync(ready)) {
      throw new Error("worker did not reach the post gate");
    }
    await expect(recovery).toContainText("leased");
    await recovery.scrollIntoViewIfNeeded();
    await page.screenshot({ path: join(shots, "leased.png"), fullPage: true });
    process.kill(-held.pid!, "SIGKILL");
    held = undefined;

    execFileSync(
      join(root, ".venv/bin/python"),
      [
        "-c",
        [
          "from sqlalchemy import create_engine, text",
          "from resolveai.testing import e2e_settings",
          "engine = create_engine(e2e_settings().database_url)",
          "with engine.begin() as conn:",
          "    conn.execute(text(",
          "        \"UPDATE app.recovery_job SET leased_until = clock_timestamp() - interval '1 second', max_attempts = 1\"",
          "    ))",
          "engine.dispose()",
        ].join("\n"),
      ],
      { cwd: root, env: { ...process.env, PYTHONPATH: "src" } },
    );

    restarted = startWorker(
      `eval "$(PYTHONPATH=src .venv/bin/python -m resolveai.testing env)" && ` +
        "PYTHONPATH=src .venv/bin/python -m resolveai.worker",
    );
    await expect(recovery).toContainText("blocked");
    await recovery.scrollIntoViewIfNeeded();
    await page.screenshot({ path: join(shots, "blocked.png"), fullPage: true });
  } finally {
    for (const child of [held, restarted]) {
      if (child?.pid) {
        try {
          process.kill(-child.pid, "SIGTERM");
        } catch {
          /* already gone */
        }
      }
    }
    rmSync(dir, { recursive: true, force: true });
  }
});
