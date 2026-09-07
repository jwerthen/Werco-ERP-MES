import { test, expect, loginAs, TEST_USERS } from './fixtures';

test('login helper prepares only the authenticated user/company tour scope before dashboard navigation', async ({
  page,
}) => {
  await page.goto('/login');
  await page.evaluate(() => {
    localStorage.setItem('werco-completed-tours:other-workspace:other-user', JSON.stringify(['quality']));
  });
  await loginAs(page, TEST_USERS.admin);
  const history = await page.evaluate(() => {
    const user = JSON.parse(sessionStorage.getItem('user') || 'null');
    const key = `werco-completed-tours:${user.company_id ?? 'workspace'}:${user.id}`;
    return {
      current: JSON.parse(localStorage.getItem(key) || '[]'),
      unrelated: JSON.parse(localStorage.getItem('werco-completed-tours:other-workspace:other-user') || '[]'),
      legacy: localStorage.getItem('werco-completed-tours'),
    };
  });
  expect(history.current).toContain('getting-started');
  expect(history.unrelated).toEqual(['quality']);
  expect(history.legacy).toBeNull();

  // The real dashboard mounts TourProvider/Layout after this full navigation.
  // An obsolete global preference lets its spotlight intercept the sidebar click.
  await page.goto('/');
  const navigation = page.getByRole('navigation', { name: 'Main navigation', exact: true });
  await expect(navigation).toBeVisible();
  await expect(page.getByRole('button', { name: 'Close tour', exact: true })).toBeHidden();
  await navigation.getByRole('link', { name: 'Work Orders', exact: true }).click();
  await expect(page).toHaveURL(/\/work-orders$/);
});
