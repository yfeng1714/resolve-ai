import { expect, type Page, test } from "@playwright/test";

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();
}

async function signOut(page: Page) {
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
}

test("live Luna approves one refund and rejects another", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$42.00", { timeout: 60_000 });
  await expect(page.getByTestId("run-budget")).toContainText("openai");
  await expect(page.getByTestId("run-budget")).toContainText("gpt-6-luna");
  await expect(page.getByTestId("run-budget")).toContainText("m2-accept-v1");
  await expect(page.getByTestId("policy-citation")).toContainText("Duplicate payment refunds");
  await expect(page.getByTestId("billing-evidence")).toContainText("INV-1001");
  await expect(page.getByTestId("billing-evidence")).toContainText("$42.00");
  await expect(page.getByText("Support cannot approve")).toBeVisible();
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
  await page.getByRole("button", { name: "Approve refund" }).click();
  await page.getByRole("button", { name: "Execute refund" }).click();
  await expect(page.getByTestId("action-status")).toContainText("confirmed");
  await expect(page.getByText("We confirmed a refund of $42.00 USD")).toBeVisible();
  await page.getByRole("button", { name: "Approve and send" }).click();
  await expect(page.getByTestId("send-receipt")).toBeVisible();
  await signOut(page);

  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$25.00", { timeout: 60_000 });
  await expect(page.getByTestId("run-budget")).toContainText("openai");
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await page.getByRole("button", { name: "Reject refund" }).click();
  await expect(page.getByText("No refund was issued")).toBeVisible();
  await expect(page.getByTestId("action-status")).toHaveCount(0);
  await page.getByRole("button", { name: "Reject reply" }).click();
  await expect(page.getByText("To blake.ortiz@customers.demo.test · rejected")).toBeVisible();
  await expect(page.getByTestId("send-receipt")).toHaveCount(0);
});
