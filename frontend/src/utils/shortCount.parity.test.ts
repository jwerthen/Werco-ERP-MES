/**
 * PARITY: the short-count arithmetic now exists in two languages.
 *
 * `utils/shortCount.ts` lets a floor screen say "2 SHORT" before the operator taps;
 * `OperationAccounting` (Python, `backend/app/services/work_order_state_service.py`) is
 * what `POST /shop-floor/operations/{id}/complete` actually decides on. Two independent
 * implementations of one rule drift silently — the kiosk would enable a COMPLETE the server
 * refuses (or disable one it would take) and nothing would fail.
 *
 * So the backend body is TRANSCRIBED here as a reference implementation and both are driven
 * through the same case table. The backend body, verbatim:
 *
 * ```python
 * @property
 * def ceiling(self) -> float:
 *     return max(0.0, self.target - self.chain_upstream)
 *
 * @property
 * def received(self) -> float:
 *     return max(0.0, self.ceiling - self.feed_short)
 *
 * @property
 * def upstream_short(self) -> float:
 *     return self.target - self.received
 *
 * @property
 * def unaccounted(self) -> float:
 *     return max(0.0, self.received - self.good - self.scrapped)
 *
 * @property
 * def closing_short(self) -> float:
 *     return max(0.0, self.received - self.good)
 *
 * @property
 * def is_accounted(self) -> bool:
 *     return self.target > QTY_EPSILON and self.unaccounted <= QTY_EPSILON
 * ```
 *
 * The client never sees `chain_upstream` / `feed_short` — the CHAIN logic is server-only —
 * only the resolved `upstream_quantity_short` (= `upstream_short`) and
 * `quantity_good_ceiling` (= `ceiling`). The table therefore drives the reference from the
 * server's inputs and the client from the two figures the server would emit for them.
 *
 * When this file fails, bring the TWO implementations back into agreement — do not adjust
 * the reference to match whatever the client happens to do.
 */

import {
  SHORT_EPSILON,
  closingShortAfterEntry,
  goodCeiling,
  operationReceived,
  operationUnaccounted,
} from './shortCount';

/** `work_order_state_service.QTY_EPSILON`. */
const BACKEND_EPSILON = 1e-9;

interface ServerInputs {
  target: number;
  good: number;
  scrapped: number;
  chainUpstream: number;
  feedShort: number;
}

/** Reference transcription of `OperationAccounting`. */
function backendAccounting(i: ServerInputs) {
  const ceiling = Math.max(0, i.target - i.chainUpstream);
  const received = Math.max(0, ceiling - i.feedShort);
  const upstreamShort = i.target - received;
  const unaccounted = Math.max(0, received - i.good - i.scrapped);
  const closingShort = Math.max(0, received - i.good);
  const isAccounted = i.target > BACKEND_EPSILON && unaccounted <= BACKEND_EPSILON;
  return { ceiling, received, upstreamShort, unaccounted, closingShort, isAccounted };
}

const CASES: Array<{ name: string } & ServerInputs> = [
  { name: 'a fresh full count', target: 10, good: 10, scrapped: 0, chainUpstream: 0, feedShort: 0 },
  { name: 'a plain short', target: 10, good: 8, scrapped: 0, chainUpstream: 0, feedShort: 0 },
  { name: 'a short covered by scrap', target: 10, good: 8, scrapped: 2, chainUpstream: 0, feedShort: 0 },
  { name: 'replacement scrap above the short', target: 10, good: 10, scrapped: 2, chainUpstream: 0, feedShort: 0 },
  { name: 'downstream of a declared upstream short', target: 10, good: 8, scrapped: 0, chainUpstream: 2, feedShort: 0 },
  { name: 'two upstream shorts', target: 10, good: 7, scrapped: 0, chainUpstream: 2, feedShort: 1 },
  { name: 'soft component feed short only', target: 10, good: 9, scrapped: 0, chainUpstream: 0, feedShort: 1 },
  { name: 'everything lost upstream', target: 5, good: 0, scrapped: 0, chainUpstream: 5, feedShort: 0 },
  { name: 'feed larger than the ceiling', target: 4, good: 0, scrapped: 0, chainUpstream: 1, feedShort: 9 },
  { name: 'target zero (never accounted)', target: 0, good: 0, scrapped: 0, chainUpstream: 0, feedShort: 0 },
  { name: 'nothing recorded yet', target: 12, good: 0, scrapped: 0, chainUpstream: 0, feedShort: 0 },
  { name: 'fractional residue', target: 0.3, good: 0.1, scrapped: 0.2, chainUpstream: 0, feedShort: 0 },
];

describe('shortCount ⇄ OperationAccounting parity', () => {
  it.each(CASES)('$name', c => {
    const server = backendAccounting(c);
    // What the floor payload carries for this operation.
    const payload = { target: c.target, upstreamShort: server.upstreamShort, ceiling: server.ceiling };

    expect(operationReceived(payload)).toBeCloseTo(server.received, 9);
    expect(goodCeiling(payload.target, payload.ceiling)).toBeCloseTo(server.ceiling, 9);

    const clientUnaccounted = operationUnaccounted({ ...payload, complete: c.good, scrapped: c.scrapped });
    const clientClosingShort = closingShortAfterEntry({ ...payload, complete: c.good });
    // The client trims residue at the SAME epsilon, so a value the server calls zero is zero here.
    expect(clientUnaccounted).toBeCloseTo(server.unaccounted <= BACKEND_EPSILON ? 0 : server.unaccounted, 9);
    expect(clientClosingShort).toBeCloseTo(server.closingShort <= BACKEND_EPSILON ? 0 : server.closingShort, 9);

    // The decision the floor screen makes ("COMPLETE is enabled") is the server's is_accounted.
    const clientAccounted = c.target > SHORT_EPSILON && clientUnaccounted <= SHORT_EPSILON;
    expect(clientAccounted).toBe(server.isAccounted);
  });

  it('uses the same epsilon as the server', () => {
    expect(SHORT_EPSILON).toBe(BACKEND_EPSILON);
  });
});
