import { test } from 'node:test';
import assert from 'node:assert/strict';
import { drainEvents, mergeEvents, resultTitle } from '../src/flow.js';

test('success title requires server confirmation of requeue', () => {
  assert.equal(resultTitle({ status: 'SUCCEEDED', disposition: 'REQUEUED', result_json: { operation: { status: 'CONFIRMED' } } }), '已重新入队');
  for (const status of ['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'FAILED']) {
    assert.notEqual(resultTitle({ status, disposition: 'REQUEUED' }), '已重新入队');
  }
  assert.notEqual(resultTitle({ status: 'SUCCEEDED', disposition: 'NO_ACTION_REQUIRED' }), '已重新入队');
});

test('event pagination drains full pages, preserves every event and deduplicates', async () => {
  let rows = [{ id: 1 }];
  const cursors = [];
  const cursor = await drainEvents(async after => {
    cursors.push(after);
    return { events: Array.from({ length: after < 200 ? 100 : 3 }, (_, i) => ({ id: after + i + 1 })) };
  }, 0, page => { rows = mergeEvents(rows, page); });
  assert.deepEqual(cursors, [0, 100, 200]);
  assert.equal(cursor, 203);
  assert.equal(rows.length, 203);
  assert.deepEqual(rows.map(r => r.id), Array.from({ length: 203 }, (_, i) => i + 1));
});

test('cancelled polling ignores late responses', async () => {
  let active = true;
  let calls = 0;
  await drainEvents(async () => { active = false; return { events: [{ id: 1 }] }; }, 0, () => calls++, () => active);
  assert.equal(calls, 0);
});
