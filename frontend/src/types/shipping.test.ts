/**
 * ``releaseExceptionsConflict`` decides whether a refusal is the structured
 * open-quality-items 409 (show the list, ask for the acknowledgment) or any other
 * refusal (show the server's text). Getting it wrong either hides the list or turns an
 * unrelated 409 -- "not shipped yet", "exceeds the remaining quantity" -- into an
 * acknowledgment prompt that cannot succeed.
 */
import { releaseExceptionsConflict, type ReleaseException } from './shipping';

const item: ReleaseException = {
  code: 'open_blocker',
  severity: 'high',
  message: "Quality hold 'Hold for review' is still open on this work order.",
  reference_type: 'work_order_blocker',
  reference_id: 4,
};

test('narrows the structured 409 and keeps the server list and message', () => {
  expect(
    releaseExceptionsConflict({
      code: 'release_exceptions_open',
      message: 'This job has open quality items.',
      release_exceptions: [item],
    })
  ).toEqual({
    code: 'release_exceptions_open',
    message: 'This job has open quality items.',
    release_exceptions: [item],
  });
});

test('a missing message becomes empty text rather than a non-string', () => {
  expect(releaseExceptionsConflict({ code: 'release_exceptions_open', release_exceptions: [] })?.message).toBe('');
});

test.each([
  ['a plain-string 409', 'A Certificate of Conformance is issued only for a shipment that has shipped.'],
  ['null', null],
  ['undefined', undefined],
  ['another structured code', { code: 'something_else', release_exceptions: [item] }],
  ['the right code with no list', { code: 'release_exceptions_open', message: 'x' }],
  ['the right code with a non-array list', { code: 'release_exceptions_open', release_exceptions: 'x' }],
])('is not the open-items conflict: %s', (_label, detail) => {
  expect(releaseExceptionsConflict(detail)).toBeNull();
});
