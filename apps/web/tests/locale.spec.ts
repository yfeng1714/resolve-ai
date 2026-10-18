import { expect, test } from "@playwright/test";

test("switches the workbench language from the header", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("radiogroup", { name: "Language" })).toHaveCount(0);
  await page.getByLabel("Email").fill("support@demo.resolveai.test");
  await page.getByLabel("Password").fill("support-demo-pass");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();

  await page.getByRole("radio", { name: "简体中文" }).click();
  await expect(page.getByRole("heading", { name: "队列" })).toBeVisible();
  await page.getByRole("radio", { name: "English" }).click();
  await expect(page.getByRole("heading", { name: "Queue" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
});
