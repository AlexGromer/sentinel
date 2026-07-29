// A representative @playwright/test suite used as an IMPORT fixture (PROD-IMPORT).
// It deliberately mixes strong locators, weak ones, a secret, a frame, and two constructs Sentinel
// has no equivalent for, so the importer's rewrite report has something real to diagnose.
import { test, expect } from '@playwright/test';

test('login and reach the dashboard', async ({ page }) => {
  await page.route('**/analytics', route => route.abort());   // network stub — no Sentinel class
  await page.goto('https://shop.example.com/login');
  await page.getByLabel('Username').fill('qa_admin');
  await page.getByLabel('Password').fill(process.env.LOGIN_PASSWORD!);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/dashboard/);
  await expect(page.getByText('Welcome back')).toBeVisible();
});

test('pay an invoice', async ({ page }) => {
  await page.goto('https://shop.example.com/billing');
  await page.getByTestId('invoice-4471').click();
  await page.locator('#pay-now').click();                     // css — weak locator
  await page.waitForTimeout(2000);                            // explicit sleep — dropped
  await page.frameLocator('iframe[name="stripe"]').getByLabel('Card number').fill('4111111111111111');
  await page.getByText('Pay now').click();                    // text — weak locator
  await expect(page.getByTestId('receipt')).toBeVisible();
});
