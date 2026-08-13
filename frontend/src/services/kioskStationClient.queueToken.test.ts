/**
 * GUARD: the crew station's queue read is authenticated by the STATION token,
 * never by a badge-minted operator token.
 *
 * WHY THIS EXISTS. The server withholds a hold blocker's free text (`title` /
 * `note`) from a crew station, and it decides "is this a station?" from the
 * caller's credential — `principal.kind == "station"` on the queue read,
 * `_token_scope == "kiosk"` on the resume response. Both are correct about the
 * credential presented. What neither can see is that the crew station holds TWO
 * credentials at once: the 24h station token AND, for a few seconds after a
 * badge scan, a 5-minute `scope="kiosk"` operator token.
 *
 * The station token is the one that gets withheld from on the READ. An operator
 * token is a normal user session there — `get_kiosk_or_user` resolves it to
 * `principal.kind == "user"` — so a queue read sent with one would come back
 * with the note and title in it, and the crew board would render an office
 * blocker's free text onto an unattended tablet. Nothing on the server can stop
 * that: from its side the request is indistinguishable from the single-operator
 * kiosk, which legitimately gets the text.
 *
 * So the gate holds by CLIENT CONVENTION at this one seam, and this file is the
 * convention written down as a test. `getQueue` takes no token parameter — the
 * type system already refuses to pass one — and it reads the station token from
 * storage itself. This asserts the second half: it does so even while an
 * operator token is live.
 *
 * The resume WRITE is the mirror image and needs no guard: it MUST be sent with
 * the operator token (the audit row records who resumed), and `_token_scope ==
 * "kiosk"` is exactly what identifies that token, so the server's gate there is
 * structural rather than conventional.
 */

import { getQueue, mintBadgeToken, setStationToken } from './kioskStationClient';

const STATION_TOKEN = 'station-token-24h';
const OPERATOR_TOKEN = 'operator-token-5min';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response;
}

describe('kioskStationClient — queue read credential', () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    sessionStorage.clear();
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    setStationToken(STATION_TOKEN);
  });

  afterEach(() => {
    jest.restoreAllMocks();
    sessionStorage.clear();
  });

  it('sends the STATION token, even while a badge-minted operator token is live', async () => {
    // Mint a badge token first — this is the real sequence on the floor: an
    // operator scans in, then the 10-15s poll fires again underneath them.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ access_token: OPERATOR_TOKEN, token_type: 'bearer', expires_in: 300 })
    );
    const minted = await mintBadgeToken('EMP-1');
    expect(minted.access_token).toBe(OPERATOR_TOKEN);

    fetchMock.mockResolvedValueOnce(jsonResponse({ queue: [], held: [] }));
    await getQueue(7);

    const [url, init] = fetchMock.mock.calls[1];
    expect(String(url)).toContain('/shop-floor/work-center-queue/7');
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Bearer ${STATION_TOKEN}`);
    // The load-bearing assertion: an operator token on this read would make the
    // server treat the unattended tablet as an identified user and send the
    // blocker free text it is supposed to withhold.
    expect(headers.Authorization).not.toContain(OPERATOR_TOKEN);
  });

  it('sends no Authorization at all when there is no station session', async () => {
    sessionStorage.clear();
    fetchMock.mockResolvedValueOnce(jsonResponse({ queue: [], held: [] }));
    await getQueue(7);

    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    // Never an operator token as a fallback — an unauthenticated 401 sends the
    // station to the PIN screen, which is the correct failure.
    expect(headers.Authorization).toBeUndefined();
  });
});
