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

test("approves one refund and rejects another without a second charge", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-1001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$42.00");
  await expect(page.getByTestId("run-budget")).toContainText("Fake model");
  await expect(page.getByTestId("policy-citation")).toContainText("Duplicate payment refunds");
  await expect(page.getByTestId("billing-evidence")).toContainText("INV-1001");
  await expect(page.getByTestId("billing-evidence")).toContainText("settled");
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
  await expect(page.getByText("Ticket reply sent")).toBeVisible();
  await signOut(page);

  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Duplicate charge on INV-2001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.locator(".amount")).toHaveText("$25.00");
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

test("a pending-payment ticket can be investigated again after an update", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Pending charge on INV-4001/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByRole("button", { name: "Investigate" })).toBeDisabled();
  await page.getByLabel("Add update").fill("The customer says the pending charge is not a duplicate.");
  await page.getByRole("button", { name: "Post update" }).click();
  await expect(page.getByText("The customer says the pending charge is not a duplicate.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Investigate" })).toBeEnabled();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByText(/Investigation · awaiting customer/)).toBeVisible();
});
