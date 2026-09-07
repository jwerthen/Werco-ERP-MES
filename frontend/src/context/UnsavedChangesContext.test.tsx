import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, Link, Outlet, RouterProvider, useNavigate } from 'react-router-dom';
import { UnsavedChangesProvider } from './UnsavedChangesContext';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
// jsdom has no Fetch Request; these routes have no loaders or network requests.
class RouterRequest {
  url: string;
  signal?: AbortSignal;
  method: string;
  constructor(url: string, init: RequestInit = {}) {
    this.url = url;
    this.signal = init.signal ?? undefined;
    this.method = init.method ?? 'GET';
  }
}
beforeAll(() => {
  global.Request = RouterRequest as unknown as typeof Request;
});
function Editor() {
  const [value, setValue] = useState('');
  const navigate = useNavigate();
  const { markSaved } = useUnsavedChanges(!!value);
  return (
    <>
      <input aria-label="Draft" value={value} onChange={event => setValue(event.target.value)} />
      <Link to="/other">Other page</Link>
      <button
        onClick={() => {
          markSaved();
          navigate('/other');
        }}
      >
        Save and leave
      </button>
    </>
  );
}
function setup() {
  const router = createMemoryRouter(
    [
      {
        element: (
          <UnsavedChangesProvider>
            <Outlet />
          </UnsavedChangesProvider>
        ),
        children: [
          { path: '/edit', element: <Editor /> },
          { path: '/other', element: <p>Destination</p> },
        ],
      },
    ],
    { initialEntries: ['/other', '/edit'], initialIndex: 1 }
  );
  render(<RouterProvider router={router} />);
  return router;
}
it('keeps edits after Stay, then follows the blocked link exactly once after discard', async () => {
  setup();
  fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'keep me' } });
  fireEvent.click(screen.getByText('Other page'));
  expect(await screen.findByRole('dialog', { name: 'Leave with unsaved changes?' })).toBeInTheDocument();
  fireEvent.click(screen.getByText('Stay and keep editing'));
  expect(screen.getByLabelText('Draft')).toHaveValue('keep me');
  fireEvent.click(screen.getByText('Other page'));
  fireEvent.click(screen.getByText('Leave and discard'));
  expect(await screen.findByText('Destination')).toBeInTheDocument();
});
it('protects browser Back and allows saved navigation without a warning', async () => {
  const router = setup();
  fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'draft' } });
  await act(async () => {
    await router.navigate(-1);
  });
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Stay and keep editing'));
  fireEvent.click(screen.getByText('Save and leave'));
  expect(await screen.findByText('Destination')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
