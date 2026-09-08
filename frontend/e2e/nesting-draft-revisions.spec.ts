import { randomUUID } from 'node:crypto';
import type { APIRequestContext } from '@playwright/test';
import { test, expect, TEST_USERS } from './fixtures';
import type { NestingDraftPage, NestingDraftRevision } from '../src/types/nestingDraft';

// This writes synthetic immutable records. CI supplies a disposable PostgreSQL
// API; refuse remote targets rather than exercising these cases on production.
const api = process.env.E2E_API_URL || 'http://127.0.0.1:8000/api/v1';
if (!['localhost', '127.0.0.1', '[::1]'].includes(new URL(api).hostname)) {
  throw new Error('Nesting revision E2E requires a local disposable test API');
}

type Auth = { headers: Record<string, string>; companyId: number };
async function login(
  request: APIRequestContext,
  email = TEST_USERS.admin.email,
  secret = TEST_USERS.admin.secret
): Promise<Auth> {
  const response = await request.post(`${api}/auth/login`, { form: { username: email, password: secret } });
  expect(response.status()).toBe(200);
  const body: { access_token: string; user: { company_id: number } } = await response.json();
  expect(typeof body.access_token).toBe('string');
  expect(Number.isInteger(body.user.company_id)).toBe(true);
  return {
    headers: { Authorization: `Bearer ${body.access_token}`, 'X-Requested-With': 'XMLHttpRequest' },
    companyId: body.user.company_id,
  };
}
function estimate(name: string) {
  return {
    version: 6,
    units: 'in',
    currency: 'USD',
    name,
    activeGroupId: 'synthetic-group',
    groups: [
      {
        id: 'synthetic-group',
        quote: {
          version: 7,
          units: 'in',
          currency: 'USD',
          name,
          material: 'Carbon steel',
          thickness: 0.125,
          margin: 0.375,
          gap: 0.125,
          objective: 'area',
          spacingMode: 'auto',
          grainAxis: 'x',
          options: [{ id: 'synthetic-stock', width: 96, height: 48, enabled: true, price: null }],
          parts: [
            {
              id: 'synthetic-plate',
              name: 'Synthetic plate',
              quantity: 1,
              rotate: true,
              color: 0,
              rotationMode: 'half-turn',
              grainAxis: 'x',
              loops: [{ type: 'circle', cx: 1, cy: 1, r: 1 }],
            },
          ],
        },
      },
    ],
  };
}
function upload(
  request: APIRequestContext,
  auth: Auth,
  value: unknown,
  key: string,
  draftId?: number,
  version?: number,
  companyId = auth.companyId
) {
  return request.post(`${api}/quote-nesting/drafts${draftId === undefined ? '' : `/${draftId}/revisions`}`, {
    headers: auth.headers,
    multipart: {
      estimate: {
        name: 'synthetic-estimate.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(value)),
      },
      request_key: key,
      expected_company_id: String(companyId),
      ...(version === undefined ? {} : { expected_version: String(version) }),
    },
  });
}
async function history(request: APIRequestContext, auth: Auth, draftId: number): Promise<NestingDraftPage> {
  const result = await request.get(`${api}/quote-nesting/drafts/${draftId}/revisions`, { headers: auth.headers });
  expect(result.status()).toBe(200);
  return result.json();
}

test('simultaneous append requests conserve history and preserve retry and historical-version semantics', async ({
  request,
}) => {
  const auth = await login(request);
  const original = estimate(`Synthetic concurrency ${randomUUID()}`);
  const created = await upload(request, auth, original, randomUUID());
  expect(created.status()).toBe(200);
  const first: NestingDraftRevision = await created.json();
  expect(first).toMatchObject({
    company_id: auth.companyId,
    revision_number: 1,
    draft_version: 1,
    status: 'DRAFT',
    estimate: original,
  });
  const candidates = ['A', 'B'].map(suffix => ({ value: estimate(`${original.name} ${suffix}`), key: randomUUID() }));
  const responses = await Promise.all(
    candidates.map(candidate => upload(request, auth, candidate.value, candidate.key, first.draft_id, 1))
  );
  expect(responses.map(response => response.status()).sort()).toEqual([200, 409]);
  const winnerIndex = responses.findIndex(response => response.status() === 200);
  const winner: NestingDraftRevision = await responses[winnerIndex].json();
  expect(winner).toMatchObject({
    revision_number: 2,
    draft_version: 2,
    status: 'DRAFT',
    estimate: candidates[winnerIndex].value,
  });
  expect((await history(request, auth, first.draft_id)).items.map(item => item.revision_number)).toEqual([2, 1]);

  const retry = await upload(
    request,
    auth,
    candidates[winnerIndex].value,
    candidates[winnerIndex].key,
    first.draft_id,
    1
  );
  expect(retry.status()).toBe(200);
  expect(await retry.json()).toEqual(winner);
  const old = await request.get(`${api}/quote-nesting/drafts/${first.draft_id}/revisions/1`, { headers: auth.headers });
  expect(old.status()).toBe(200);
  expect(await old.json()).toEqual(first);
  expect((await upload(request, auth, original, randomUUID(), first.draft_id, 1)).status()).toBe(409);
  expect((await upload(request, auth, { ...original, units: 'mm' }, randomUUID(), first.draft_id, 2)).status()).toBe(
    422
  );
  expect(
    (await upload(request, auth, original, randomUUID(), first.draft_id, 2, auth.companyId + 1000000)).status()
  ).toBe(409);
  expect((await upload(request, auth, original, candidates[winnerIndex].key, first.draft_id, 1)).status()).toBe(409);
  expect((await history(request, auth, first.draft_id)).total).toBe(2);
});

test('concurrent same-key creation returns one revision and a different tenant cannot read or append it', async ({
  request,
}) => {
  const auth = await login(request);
  const value = estimate(`Synthetic retry ${randomUUID()}`),
    key = randomUUID();
  const replies = await Promise.all([upload(request, auth, value, key), upload(request, auth, value, key)]);
  expect(replies.map(reply => reply.status())).toEqual([200, 200]);
  const first: NestingDraftRevision = await replies[0].json();
  expect(await replies[1].json()).toEqual(first);
  expect((await history(request, auth, first.draft_id)).total).toBe(1);

  // The seeded administrator is a platform-capable fixture; this creates only
  // an isolated synthetic company/admin, with no email or operational records.
  const stamp = randomUUID(),
    email = `nest-${stamp}@example.com`,
    secret = randomUUID();
  const company = await request.post(`${api}/platform/companies`, {
    headers: auth.headers,
    data: {
      name: `Synthetic nesting tenant ${stamp}`,
      slug: `nest-${stamp}`,
      admin_email: email,
      admin_first_name: 'Synthetic',
      admin_last_name: 'Reviewer',
      admin_password: secret,
    },
  });
  expect(company.status()).toBe(200);
  const foreign = await login(request, email, secret);
  expect(foreign.companyId).not.toBe(auth.companyId);
  for (const path of [`${first.draft_id}/revisions`, `${first.draft_id}/revisions/1`]) {
    expect((await request.get(`${api}/quote-nesting/drafts/${path}`, { headers: foreign.headers })).status()).toBe(404);
  }
  expect((await upload(request, foreign, value, randomUUID(), first.draft_id, 1)).status()).toBe(404);
  const empty = await request.get(`${api}/quote-nesting/drafts`, { headers: foreign.headers });
  expect(empty.status()).toBe(200);
  expect((await empty.json()).total).toBe(0);
  const own = await upload(request, foreign, value, key);
  expect(own.status()).toBe(200);
  const ownRevision: NestingDraftRevision = await own.json();
  expect(ownRevision.company_id).toBe(foreign.companyId);
  expect(ownRevision.draft_id).not.toBe(first.draft_id);
  expect((await history(request, auth, first.draft_id)).total).toBe(1);
});
