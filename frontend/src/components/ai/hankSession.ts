/**
 * UI invalidation only: authorization always remains with the API. Ignore token
 * rotation timestamps so refreshing the same session keeps Hank's conversation.
 */
function claims(): Record<string, unknown> | null {
  try {
    const token = sessionStorage.getItem('token');
    if (!token) return null;
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : null;
  } catch {
    return null;
  }
}

export function getHankSessionScope(): string | null {
  const payload = claims();
  if (!payload) return null;
  return JSON.stringify([payload.sub, payload.cid, payload.ro === true, payload.scope, payload.type]);
}

export function isHankReadOnlySession(): boolean {
  return claims()?.ro === true;
}

export function subscribeHankSession(onChange: () => void): () => void {
  window.addEventListener('werco:auth-token-changed', onChange);
  return () => window.removeEventListener('werco:auth-token-changed', onChange);
}
