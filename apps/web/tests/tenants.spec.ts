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

test("each tenant sees its own queue and cannot open the other tenant's ticket", async ({
  page,
}) => {
  await signIn(page, "support@demo.resolveai.test", "support-demo-pass");
  await expect(
    page.getByText("The queue is a synthetic billing sample. Nothing here is a live payment."),
  ).toBeVisible();
  await expect(page.getByText(/Northwind/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Duplicate charge on INV-1001/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Duplicate charge on INV-3001/ })).toHaveCount(0);
  const hiddenHarbor = await page.request.get(
    "/api/tickets/55555555-5555-4555-8555-555555555511",
  );
  expect(hiddenHarbor.status()).toBe(404);
  await signOut(page);

  await signIn(page, "support.harbor@demo.resolveai.test", "support-harbor-pass");
  await expect(
    page.getByText("The queue is a synthetic billing sample. Nothing here is a live payment."),
  ).toBeVisible();
  await expect(page.getByText(/Northwind/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Duplicate charge on INV-3001/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Duplicate charge on INV-1001/ })).toHaveCount(0);
  const hiddenNorthwind = await page.request.get(
    "/api/tickets/55555555-5555-4555-8555-555555555501",
  );
  expect(hiddenNorthwind.status()).toBe(404);
});
