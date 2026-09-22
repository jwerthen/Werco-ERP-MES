import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { CopilotPanelProps } from './ai/CopilotPanel';

jest.mock('./CompanySwitcher', () => ({ __esModule: true, default: () => null }));
jest.mock('./ReadOnlyBanner', () => ({ __esModule: true, default: () => null }));
jest.mock('./SessionWarningModal', () => ({ __esModule: true, default: () => null }));
jest.mock('./SkipLink', () => ({ __esModule: true, default: () => null }));
jest.mock('./AdaptivePromptPanel', () => ({ __esModule: true, default: () => null }));
jest.mock('./Tour', () => ({ TourMenu: () => null }));
jest.mock('./ui/BottomNav', () => ({ __esModule: true, default: () => null }));
jest.mock('./NotificationBell', () => ({ __esModule: true, default: () => null }));
jest.mock('./GlobalSearch', () => ({
  __esModule: true,
  default: () => null,
  useGlobalSearch: () => ({ isOpen: false, open: jest.fn(), close: jest.fn() }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({}) }));
jest.mock('../hooks/useScrollRestoration', () => ({ useScrollRestoration: () => undefined }));
jest.mock('../hooks/useKeyboardShortcuts', () => ({ useKeyboardShortcuts: () => undefined, GLOBAL_SHORTCUTS: [] }));
jest.mock('../context/KeyboardShortcutsContext', () => ({
  useKeyboardShortcutsContext: () => ({ showHelp: jest.fn() }),
}));
jest.mock('../services/realtime', () => ({ buildWsUrl: () => 'ws://localhost/ws', getAccessToken: () => 'token' }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getPendingUserApprovalSummary: jest.fn().mockResolvedValue({ count: 0 }) },
}));
jest.mock('../context/TourContext', () => ({ useTour: () => ({ startTour: jest.fn(), isTourComplete: () => true }) }));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: {
      id: 17,
      role: 'manager',
      is_superuser: false,
      first_name: 'Shop',
      last_name: 'Employee',
      email: 'shop@example.invalid',
    },
    logout: jest.fn(),
    logoutWithEmployeeId: jest.fn(),
  }),
}));
const mockPanelUnmount = jest.fn();
jest.mock('./ai/CopilotPanel', () => ({
  CopilotPanel: ({ isOpen }: CopilotPanelProps) => {
    const [draft, setDraft] = React.useState('');
    React.useEffect(() => () => mockPanelUnmount(), []);
    return (
      <section aria-label="Hank session probe">
        <p>{isOpen ? 'Hank drawer open' : 'Hank drawer closed'}</p>
        <input aria-label="Hank private draft" value={draft} onChange={event => setDraft(event.target.value)} />
      </section>
    );
  },
}));
import Layout from './Layout';

function token(cid = 4, ro = false, exp = 100) {
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid, ro, type: 'access', exp }))}.signature`
  );
}
function changed() {
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
}
function show(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Layout>
        <p>ERP workspace</p>
      </Layout>
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  token();
});

it.each(['intake', 'handoff', 'routine'])('opens Hank for a saved %s deep link', async area => {
  show(`/?hank_work=${area}&hank_id=19`);
  expect(await screen.findByText('Hank drawer open')).toBeInTheDocument();
});

it.each([
  { cid: 5, ro: false },
  { cid: 4, ro: true },
])('remounts all Hank state when effective session changes to %j', ({ cid, ro }) => {
  show();
  fireEvent.change(screen.getByLabelText('Hank private draft'), { target: { value: 'Private job data' } });
  token(cid, ro);
  changed();
  expect(screen.getByLabelText('Hank private draft')).toHaveValue('');
  expect(mockPanelUnmount).toHaveBeenCalledTimes(1);
});

it('preserves the current Hank draft when a token refresh keeps the same actor and authority', () => {
  show();
  fireEvent.change(screen.getByLabelText('Hank private draft'), { target: { value: 'Unfinished review' } });
  token(4, false, 200);
  changed();
  expect(screen.getByLabelText('Hank private draft')).toHaveValue('Unfinished review');
  expect(mockPanelUnmount).not.toHaveBeenCalled();
});

it('clears Hank state when the session token is removed', () => {
  show();
  fireEvent.change(screen.getByLabelText('Hank private draft'), { target: { value: 'Private job data' } });
  sessionStorage.removeItem('token');
  changed();
  expect(screen.getByLabelText('Hank private draft')).toHaveValue('');
  expect(mockPanelUnmount).toHaveBeenCalledTimes(1);
});
