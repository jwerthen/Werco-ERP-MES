/**
 * MySettings — the per-user notification settings page (`/settings`).
 *
 * PR 4 ships the SMS slice only, so this covers the page shell: the heading, the
 * cross-link back to the inbox, that the SMS section is mounted, and that the
 * route resolves a real title in routeMeta (so the top bar / breadcrumbs don't
 * fall back to the unknown-route label). The section itself is mocked here — its
 * behavior is covered in components/settings/SmsSettingsSection.test.tsx.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import MySettings from './MySettings';
import { getRouteTitle } from '../utils/routeMeta';

jest.mock('../components/settings/SmsSettingsSection', () => ({
  __esModule: true,
  default: () => <div data-testid="sms-settings-section" />,
}));

const renderPage = () =>
  render(
    <MemoryRouter>
      <MySettings />
    </MemoryRouter>,
  );

describe('MySettings', () => {
  it('renders the page shell and mounts the SMS settings section', () => {
    renderPage();

    expect(screen.getByRole('heading', { name: /my settings/i })).toBeInTheDocument();
    expect(screen.getByTestId('sms-settings-section')).toBeInTheDocument();
  });

  it('cross-links back to the notification inbox', () => {
    renderPage();

    expect(screen.getByRole('link', { name: /view notifications/i })).toHaveAttribute(
      'href',
      '/notifications',
    );
  });

  it('resolves a real page title from routeMeta', () => {
    expect(getRouteTitle({ pathname: '/settings', search: '' })).toBe('My Settings');
  });
});
