import { hankRecordContext } from './hankContext';

it.each([
  ['/work-orders/7', '', { workOrderId: 7 }],
  ['/parts/8', '', { partId: 8 }],
  ['/purchasing', '?po=11', { purchaseOrderId: 11 }],
  ['/receiving', '?po=12', { purchaseOrderId: 12 }],
  ['/warehouse', '?tab=receiving&po=13', { purchaseOrderId: 13 }],
] as const)('uses exact readable record context on %s %s', (path, search, expected) => {
  expect(hankRecordContext(path, search)).toMatchObject(expected);
});

it.each([
  ['/work-orders/0', ''],
  ['/work-orders/-1', ''],
  ['/work-orders/1.5', ''],
  ['/work-orders/01', ''],
  ['/work-orders/9007199254740992', ''],
  ['/work-orders/7/edit', ''],
  ['/purchasing', '?po=0'],
  ['/purchasing', '?po=1e2'],
  ['/purchasing', '?po=9007199254740992'],
  ['/purchasing', '?po=01'],
  ['/warehouse', '?tab=inventory&po=11'],
  ['/quality', '?po=11'],
  ['/', '?work_order_id=7&po=11'],
])('does not infer unrelated or invalid record context on %s %s', (path, search) => {
  const context = hankRecordContext(path, search);
  expect(context.workOrderId).toBeUndefined();
  expect(context.purchaseOrderId).toBeUndefined();
  expect(context.partId).toBeUndefined();
});

it('does not carry a PO query parameter into a job page context', () => {
  expect(hankRecordContext('/work-orders/7', '?po=11')).toEqual({
    workOrderId: 7,
    partId: undefined,
    purchaseOrderId: undefined,
  });
});
