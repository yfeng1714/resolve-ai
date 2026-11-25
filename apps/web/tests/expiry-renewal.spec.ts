import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Page, test } from "@playwright/test";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "../../..");

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();
}

test("renews an expired unqueued approval from the workbench", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$25.00");
  await page.getByRole("button", { name: "Sign out" }).click();

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await page.getByRole("button", { name: "Approve refund" }).click();
  const grant = page.getByTestId("grant-status");
  await expect(grant).toContainText("valid");
  await expect(grant).toContainText("Valid until");
  const approvalId = await grant.getAttribute("data-approval-id");
  expect(approvalId).toBeTruthy();
  execFileSync(
    join(root, ".venv/bin/python"),
    [
      "-c",
      [
        "from uuid import UUID",
        "from sqlalchemy import create_engine, text",
        "from resolveai.testing import e2e_settings",
        "engine = create_engine(e2e_settings().database_url)",
        "with engine.begin() as conn:",
        "    conn.execute(text(",
        `        "UPDATE app.approval SET expires_at = clock_timestamp() - interval '1 second' WHERE id = '${approvalId}'"`,
        "    ))",
        "engine.dispose()",
      ].join("\n"),
    ],
    { cwd: root, env: { ...process.env, PYTHONPATH: "src" } },
  );
  await page.reload();
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await expect(page.getByTestId("grant-status")).toContainText("expired");
  await page.getByTestId("renew-approval").click();
  await expect(page.getByTestId("grant-status")).toContainText("valid");
  await expect(page.getByTestId("grant-history").locator("li")).toHaveCount(2);
  await expect(page.getByTestId("action-status")).toHaveCount(0);
});
