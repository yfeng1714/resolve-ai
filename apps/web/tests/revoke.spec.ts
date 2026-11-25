import { expect, type Page, test } from "@playwright/test";

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();
}

test("revokes an approval before admission and shows the reason", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$42.00");
  await page.getByRole("button", { name: "Sign out" }).click();

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
  await page.getByRole("button", { name: "Approve refund" }).click();
  await expect(page.getByTestId("grant-status")).toContainText("valid");
  await page.getByTestId("revoke-reason").fill("Customer withdrew before dispatch.");
  await page.getByRole("button", { name: "Revoke approval" }).click();
  await expect(page.getByTestId("grant-status")).toContainText("revoked");
  await expect(page.getByTestId("grant-status")).toContainText("Customer withdrew before dispatch.");
  await expect(page.getByRole("button", { name: "Execute refund" })).toHaveCount(0);
  await expect(page.getByTestId("action-status")).toHaveCount(0);
  await expect(page.getByTestId("billing-evidence")).toContainText("None");
});
