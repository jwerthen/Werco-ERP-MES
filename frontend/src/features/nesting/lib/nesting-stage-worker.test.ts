import { compareProjectStagesInWorker } from './nesting-worker-client';
import { remnantStageFixture } from '../../../test-utils/remnantStageFixtures';
import type { RemnantStageMessage, RemnantSummaryMessage } from './remnant-planning';
type Reply = RemnantStageMessage | RemnantSummaryMessage | { ok: false; error: string };
type Controlled = {
  onmessage: ((event: MessageEvent<Reply>) => void) | null;
  onerror: (() => void) | null;
  postMessage: jest.Mock;
  terminate: jest.Mock;
};
const mockWorkers: Controlled[] = [];
jest.mock('./nesting.worker?worker', () => ({
  __esModule: true,
  default: jest.fn().mockImplementation(() => {
    const worker: Controlled = { onmessage: null, onerror: null, postMessage: jest.fn(), terminate: jest.fn() };
    mockWorkers.push(worker);
    return worker;
  }),
}));
const reply = (data: Reply) => mockWorkers[0].onmessage?.(new MessageEvent('message', { data }));
afterEach(() => {
  jest.useRealTimers();
  mockWorkers.length = 0;
});
test('one worker streams a bounded ordered plan and checks completion excludes the prerequisite', async () => {
  const f = await remnantStageFixture();
  const onStage = jest.fn();
  const pending = compareProjectStagesInWorker(f.raw, f.digest, { onStage });
  for (const stage of f.stages) reply(stage);
  reply(f.summary);
  await expect(pending).resolves.toEqual(f.summary);
  expect(onStage).toHaveBeenCalledTimes(3);
  expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
  reply(f.stages[0]);
  expect(onStage).toHaveBeenCalledTimes(3);
});
test.each(['cancel', 'timeout'] as const)(
  'retains delivered baselines but rejects late progress after %s',
  async mode => {
    const f = await remnantStageFixture();
    jest.useFakeTimers();
    const onStage = jest.fn(),
      controller = new AbortController();
    const pending = compareProjectStagesInWorker(f.raw, f.digest, { signal: controller.signal, onStage });
    const rejection = expect(pending).rejects.toThrow(/cancelled|limit/i);
    reply(f.stages[0]);
    if (mode === 'cancel') controller.abort();
    else jest.advanceTimersByTime(120000);
    await rejection;
    reply(f.stages[1]);
    expect(onStage).toHaveBeenCalledTimes(1);
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  }
);
test('rejects wrong stage order and incorrect completed alternative count', async () => {
  const f = await remnantStageFixture();
  let pending = compareProjectStagesInWorker(f.raw, f.digest, { onStage: jest.fn() });
  reply(f.stages[1]);
  await expect(pending).rejects.toThrow(/unexpected stage/);
  mockWorkers.length = 0;
  pending = compareProjectStagesInWorker(f.raw, f.digest, { onStage: jest.fn() });
  f.stages.forEach(reply);
  reply({ ...f.summary, complete_option_count: 3 });
  await expect(pending).rejects.toThrow(/inconsistent/);
});
