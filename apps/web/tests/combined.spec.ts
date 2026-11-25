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

test("keeps a confirmed refund after a later investigation", async ({ page }) => {
  await signIn(page, "support.harbor@demo.resolveai.test", "support-harbor-pass");
  await page.getByRole("button", { name: /Harbor refund and schedule/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("combined-proposal")).toHaveCount(2);
  await signOut(page);

  await signIn(page, "approver.harbor@demo.resolveai.test", "approver-harbor-pass");
  await page.getByRole("button", { name: /Harbor refund and schedule/ }).click();
  await page.getByRole("button", { name: "Approve refund" }).click();
  await page.getByRole("button", { name: "Execute refund" }).click();
  await expect(page.getByTestId("combined-halves")).toContainText("refund · confirmed");
  await expect(page.getByTestId("combined-halves")).toContainText("cancel_at_period_end · open");
  await signOut(page);

  await signIn(page, "support.harbor@demo.resolveai.test", "support-harbor-pass");
  await page.getByRole("button", { name: /Harbor refund and schedule/ }).click();
  await page.getByLabel("Add update").fill("Please look at the request again.");
  await page.getByRole("button", { name: "Post update" }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("combined-halves")).toContainText("refund · confirmed");
  await expect(page.getByTestId("combined-proposal")).toHaveCount(2);
  await expect(page.getByTestId("cancellation-state")).toContainText("Not scheduled yet");
  await expect(page.getByTestId("action-status")).toContainText("confirmed");
});

test("approves the refund and the cancellation separately", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Refund and cancel/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("combined-halves")).toContainText("refund · open");
  await expect(page.getByTestId("combined-halves")).toContainText("cancel_at_period_end · open");
  await expect(page.getByTestId("combined-proposal")).toHaveCount(2);
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Refund and cancel/ }).click();
  await page.getByRole("button", { name: "Approve refund" }).click();
  await page.getByRole("button", { name: "Execute refund" }).click();
  await expect(page.getByTestId("combined-halves")).toContainText("refund · confirmed");
  await expect(page.getByTestId("combined-halves")).toContainText("cancel_at_period_end · open");
  await page.getByRole("button", { name: "Approve cancellation" }).click();
  await page.getByRole("button", { name: "Schedule cancellation" }).click();
  await expect(page.getByTestId("combined-halves")).toContainText("cancel_at_period_end · confirmed");
  await expect(page.getByRole("button", { name: "Approve and send" })).toBeVisible();
  await page.getByRole("button", { name: "Reject reply" }).click();
  await expect(page.getByRole("button", { name: "Approve and send" })).toBeVisible();
  await expect(page.getByTestId("combined-halves")).toContainText("refund · confirmed");
  await expect(page.getByTestId("combined-halves")).toContainText("cancel_at_period_end · confirmed");
});
