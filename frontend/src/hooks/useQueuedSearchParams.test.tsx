import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { useQueuedSearchParams } from './useQueuedSearchParams';

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
const originalRequest = global.Request;
beforeAll(() => {
  global.Request = RouterRequest as unknown as typeof Request;
});
afterAll(() => {
  global.Request = originalRequest;
});

function Controls() {
  const [params, setParams] = useQueuedSearchParams();
  const set = (key: string, value: string) =>
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  return (
    <>
      <output>{params.toString()}</output>
      <button onClick={() => set('customer', '')}>Clear customer</button>
      <button onClick={() => set('group', 'customer')}>Group</button>
      <button onClick={() => set('status', 'released')}>Released</button>
    </>
  );
}

test('rapid pending data-router filter changes merge; Back restores the committed query', async () => {
  const loads: (() => void)[] = [];
  const router = createMemoryRouter(
    [{ path: '/work-orders', element: <Controls />, loader: () => new Promise<void>(resolve => loads.push(resolve)) }],
    { initialEntries: ['/work-orders?customer=Acme&search=fixture'] }
  );
  render(<RouterProvider router={router} />);
  await act(async () => loads.shift()!());
  fireEvent.click(screen.getByRole('button', { name: 'Clear customer' }));
  fireEvent.click(screen.getByRole('button', { name: 'Group' }));
  fireEvent.click(screen.getByRole('button', { name: 'Released' }));
  await act(async () => {
    loads.splice(0).forEach(resolve => resolve());
  });
  await waitFor(() =>
    expect(screen.getByRole('status')).toHaveTextContent('search=fixture&group=customer&status=released')
  );
  act(() => {
    void router.navigate(-1);
  });
  await act(async () => {
    loads.splice(0).forEach(resolve => resolve());
  });
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('customer=Acme&search=fixture'));
  router.dispose();
});
