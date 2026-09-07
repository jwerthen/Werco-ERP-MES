import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  createMemoryRouter,
  MemoryRouter,
  RouterProvider,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import Warehouse from './Warehouse';
import { UnsavedChangesProvider } from '../context/UnsavedChangesContext';
import api from '../services/api';
jest.mock('./Inventory', () => ({ __esModule: true, default: () => <div>Inventory panel</div> }));
jest.mock('./Shipping', () => ({ __esModule: true, default: () => <div>Shipping panel</div> }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: 'manager' } }) }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
  },
}));
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
function Location() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="Current URL">{location.search}</output>
      <button onClick={() => navigate(-1)}>Browser back</button>
    </>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  const mocked = api as jest.Mocked<typeof api>;
  mocked.getOpenPOsForReceiving.mockResolvedValue([]);
  mocked.getReceivingLocations.mockResolvedValue([]);
  mocked.getReceivingStats.mockResolvedValue({});
  mocked.getInspectionQueue.mockResolvedValue([]);
  mocked.getReceivingHistory.mockResolvedValue([]);
});
test('nested Receiving tabs preserve outer warehouse selection, filters, reload and Back', async () => {
  render(
    <MemoryRouter initialEntries={['/warehouse?tab=receiving&receivingSearch=ACME']}>
      <Location />
      <Warehouse />
    </MemoryRouter>
  );
  const queue = await screen.findByRole('tab', { name: /Inspection Queue/ });
  fireEvent.click(queue);
  expect(screen.getByLabelText('Current URL')).toHaveTextContent('tab=receiving');
  expect(screen.getByLabelText('Current URL')).toHaveTextContent('receivingTab=queue');
  expect(screen.getByLabelText('Current URL')).toHaveTextContent('receivingSearch=ACME');
  expect(screen.getByRole('tab', { name: /Receiving & Inspection/ })).toHaveAttribute('aria-selected', 'true');
  expect(await screen.findByRole('heading', { name: 'Items Pending Inspection' })).toBeInTheDocument();
  fireEvent.keyDown(queue, { key: 'ArrowRight' });
  expect(await screen.findByRole('heading', { name: /Receiving History/ })).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: 'History' })).toHaveFocus();
  fireEvent.click(screen.getByRole('button', { name: 'Browser back' }));
  await waitFor(() =>
    expect(screen.getByRole('tab', { name: /Inspection Queue/ })).toHaveAttribute('aria-selected', 'true')
  );
});

// The app uses a data router around descendant Routes. Its navigation updates run
// in a React transition; browser Back can arrive before that transition commits.
test.each(['History', 'Shipping'])(
  'rapid Back during the %s data-router transition restores both tab levels',
  async target => {
    const router = createMemoryRouter(
      [
        {
          path: '*',
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route
                  path="/warehouse"
                  element={
                    <>
                      <Location />
                      <Warehouse />
                    </>
                  }
                />
              </Routes>
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: ['/warehouse?tab=receiving&receivingSearch=ACME'] }
    );
    render(
      <React.StrictMode>
        <RouterProvider router={router} />
      </React.StrictMode>
    );
    fireEvent.click(await screen.findByRole('tab', { name: /Inspection Queue/ }));
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /Inspection Queue/ })).toHaveAttribute('aria-selected', 'true')
    );
    act(() => {
      screen.getByRole('tab', { name: target }).click();
      void router.navigate(-1);
    });
    await waitFor(() => expect(router.state.location.search).toContain('receivingTab=queue'));
    expect(screen.getByRole('tab', { name: /Inspection Queue/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('heading', { name: 'Items Pending Inspection' })).toBeInTheDocument();
    expect(screen.getByLabelText('Current URL')).toHaveTextContent('receivingSearch=ACME');
    expect(screen.getByRole('tab', { name: /Receiving & Inspection/ })).toHaveAttribute('aria-selected', 'true');
    const key = router.state.location.key;
    fireEvent.click(screen.getByRole('tab', { name: /Inspection Queue/ }));
    fireEvent.click(screen.getByRole('tab', { name: /Receiving & Inspection/ }));
    expect(router.state.location.key).toBe(key);
  }
);
