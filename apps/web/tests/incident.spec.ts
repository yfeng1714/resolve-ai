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

test("a known incident waits for the report and then names the record", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /API failures on the known incident/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("incident-report")).toBeVisible();
  await expect(page.getByTestId("incident-state")).toHaveCount(0);
  await page.getByLabel("Request reference").fill("REQ-1");
  await page.getByRole("button", { name: "Record incident report" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("confirmed · record_match");
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /API failures on the known incident/ }).click();
  await page.getByRole("button", { name: "Approve and send" }).click();
  await expect(page.getByTestId("send-receipt")).toBeVisible();
});

test("a new update hides the unsent incident draft", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /API failures with no incident/ }).click();
  await page.getByRole("button", { name: "Record incident report" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("none · no_overlap");
  await page.getByLabel("Add update").fill("There is an API failure.");
  await page.getByRole("button", { name: "Post update" }).click();
  await expect(page.getByTestId("incident-state")).toHaveCount(0);
  await expect(page.getByTestId("incident-history")).toBeVisible();
});

test("support can replace a recorded incident report", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /API failures after resolution/ }).click();
  await page.getByRole("button", { name: "Record incident report" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("possible · resolved_before_report");
  await page.getByRole("button", { name: "Correct incident report" }).click();
  await page
    .getByTestId("incident-report-correction")
    .getByLabel("Customer-reported start")
    .fill("2026-10-10T00:00:00.000000Z");
  await page.getByRole("button", { name: "Record corrected report" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("confirmed · record_match");
  await expect(page.getByTestId("incident-history")).toContainText("possible");
  await expect(page.getByLabel("Add update")).toBeVisible();
});

test("an approver can reject an incident reply and support can run it again", async ({ page }) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /API failures with two incidents/ }).click();
  await page.getByRole("button", { name: "Record incident report" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("several · several_matches");
  await signOut(page);

  await signIn(page, "approver@demo.resolveai.test", "approver-demo-pass");
  await page.getByRole("button", { name: /API failures with two incidents/ }).click();
  await page.getByRole("button", { name: "Reject reply" }).click();
  await expect(page.getByRole("button", { name: "Approve and send" })).toHaveCount(0);
  await signOut(page);

  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await page.getByRole("button", { name: /API failures with two incidents/ }).click();
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByTestId("incident-state")).toHaveText("several · several_matches");
});
