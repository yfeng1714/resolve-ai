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

test("schedules one period end and leaves an unconfirmed proposal unscheduled", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Cancel the Standard plan/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("cancellation-state")).toHaveText(
    "Period-end cancellation proposed. Not scheduled yet.",
  );
  await expect(page.getByText("Cancellation scheduled for")).toHaveCount(0);
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Cancel the Standard plan/ }).click();
  await page.getByRole("button", { name: "Approve cancellation" }).click();
  await page.getByRole("button", { name: "Schedule cancellation" }).click();
  await expect(page.getByTestId("recovery-state")).toContainText("settled");
  await expect(page.getByTestId("cancellation-state")).toHaveText("Cancellation scheduled");
  await expect(page.getByText(/Access continues until then/)).toBeVisible();
  await signOut(page);

  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Already ending/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByText("Cancellation scheduled")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Schedule cancellation" })).toHaveCount(0);
});

test("a rejection does not schedule the cancellation", async ({ page }) => {
  await signIn(page, "support.harbor@demo.resolveai.test", "support-harbor-pass");
  await page.getByRole("button", { name: /Cancel the Team plan/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("cancellation-state")).toHaveText(
    "Period-end cancellation proposed. Not scheduled yet.",
  );
  await signOut(page);

  await signIn(page, "approver.harbor@demo.resolveai.test", "approver-harbor-pass");
  await page.getByRole("button", { name: /Cancel the Team plan/ }).click();
  await page.getByRole("button", { name: "Reject cancellation" }).click();
  await expect(page.getByText("No cancellation was scheduled")).toBeVisible();
  await expect(page.getByTestId("cancellation-state")).toHaveText(
    "Period-end cancellation proposed. Not scheduled yet.",
  );
});

test("an earlier confirmation does not mark the next subscription scheduled", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Which plan should end/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await page.getByTestId("choose-subscription").filter({ hasText: "plus" }).click();
  await expect(page.getByTestId("cancellation-state")).toContainText("Not scheduled yet");
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /Which plan should end/ }).click();
  await page.getByRole("button", { name: "Approve cancellation" }).click();
  await page.getByRole("button", { name: "Schedule cancellation" }).click();
  await expect(page.getByTestId("recovery-state")).toContainText("settled");
  await expect(page.getByTestId("cancellation-state")).toHaveText("Cancellation scheduled");
  await page.getByRole("button", { name: "Approve and send" }).click();
  await expect(page.getByTestId("send-receipt")).toBeVisible();
  await signOut(page);

  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /Which plan should end/ }).click();
  await page.getByTestId("choose-subscription").filter({ hasText: "standard" }).click();
  await expect(page.getByTestId("cancellation-state")).toContainText("Not scheduled yet");
  await expect(page.getByTestId("recovery-state")).toHaveCount(0);
  await expect(page.getByTestId("cancellation-history")).toContainText("confirmed");
});
