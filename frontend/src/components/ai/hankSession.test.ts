import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from './hankSession';

const normalSession = { sub: '17', cid: 4, ro: false, scope: 'erp', type: 'access' };

function token(payload: unknown) {
  const base64url = btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  sessionStorage.setItem('token', `header.${base64url}.signature`);
}

beforeEach(() => sessionStorage.clear());

describe('Hank session invalidation', () => {
  it('keeps the same conversation identity across token refreshes', () => {
    token({ ...normalSession, iat: 10, exp: 100, jti: 'old' });
    const beforeRefresh = getHankSessionScope();
    token({ ...normalSession, iat: 50, exp: 150, jti: 'new' });
    expect(getHankSessionScope()).toBe(beforeRefresh);
  });

  it.each([
    ['user', { sub: '18' }],
    ['company', { cid: 5 }],
    ['read-only state', { ro: true }],
    ['scope', { scope: 'kiosk' }],
    ['credential type', { type: 'api' }],
  ])('invalidates the conversation when its %s changes', (_label, change) => {
    token(normalSession);
    const beforeChange = getHankSessionScope();
    token({ ...normalSession, ...change });
    expect(getHankSessionScope()).not.toBe(beforeChange);
  });

  it('clears identity at logout and accepts older tokens without the optional read-only claim', () => {
    token({ sub: '17', cid: 4, scope: 'erp', type: 'access' });
    const before = getHankSessionScope();
    token(normalSession);
    expect(getHankSessionScope()).toBe(before);
    sessionStorage.removeItem('token');
    expect(getHankSessionScope()).toBeNull();
  });

  it('recognizes only an explicit boolean read-only claim', () => {
    token({ ...normalSession, ro: true });
    expect(isHankReadOnlySession()).toBe(true);
    token({ ...normalSession, ro: 'true' });
    expect(isHankReadOnlySession()).toBe(false);
    token(normalSession);
    expect(isHankReadOnlySession()).toBe(false);
  });

  it.each(['not-a-token', 'header.%%%bad%%%.signature', 'header.bnVsbA.signature', 'header.W10.signature'])(
    'handles malformed or unusable token %s without throwing',
    value => {
      sessionStorage.setItem('token', value);
      expect(getHankSessionScope()).toBeNull();
      expect(isHankReadOnlySession()).toBe(false);
    }
  );

  it('notifies React on token changes and removes its listener when unsubscribed', () => {
    const onChange = jest.fn();
    const unsubscribe = subscribeHankSession(onChange);
    window.dispatchEvent(new Event('werco:auth-token-changed'));
    expect(onChange).toHaveBeenCalledTimes(1);
    unsubscribe();
    window.dispatchEvent(new Event('werco:auth-token-changed'));
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
