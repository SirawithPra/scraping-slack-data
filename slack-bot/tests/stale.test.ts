/**
 * The working-day count, and the rule that keeps the escalation from becoming noise.
 *
 * Two failures this is written against. The loud one: counting calendar days, so every
 * Friday item crosses the threshold two days early on Monday morning and the channel
 * gets a card about nothing — which is how a bot earns a mute, and a muted bot loses
 * the announcements that were real. The quiet one: bucketing wrong so an item that has
 * been silent for a month is announced once and then never again, exactly when it is
 * most worth mentioning.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { businessDaysBetween, isWeekend, staleItems, staleKey } from '../src/stale.js';
import type { WorkItem } from '../src/types.js';

/** Local time on purpose: the whole feature runs off the host's clock, and so must this. */
const at = (iso: string) => new Date(`${iso}T09:00:00`);

function item(over: Partial<WorkItem> = {}): WorkItem {
  return {
    key: 'REV-1',
    headline: 'หน้า redemption',
    state: 'moving',
    evidence: '',
    evidence_id: 'm1',
    age_days: 1,
    participants: [],
    sources: {},
    first: '2026-08-01 09:00',
    last: '2026-08-14 09:00',
    timeline: [],
    messages: [],
    ...over,
  };
}

test('a weekend adds nothing to the count', () => {
  // Friday 2026-08-14 → Monday 2026-08-17 is one working day, not three. Getting this
  // wrong is what makes the escalation fire every Monday about work that was fine.
  assert.equal(businessDaysBetween(at('2026-08-14'), at('2026-08-17')), 1);
  assert.ok(isWeekend(at('2026-08-15')) && isWeekend(at('2026-08-16')));
});

test('a full week of working days is five', () => {
  assert.equal(businessDaysBetween(at('2026-08-14'), at('2026-08-21')), 5);
});

test('the same day, and a clock running backwards, are both zero', () => {
  assert.equal(businessDaysBetween(at('2026-08-14'), at('2026-08-14')), 0);
  // Never negative: a skewed clock must not produce an escalation with a nonsense
  // number printed on it.
  assert.equal(businessDaysBetween(at('2026-08-21'), at('2026-08-14')), 0);
});

test('finished work is allowed to go quiet', () => {
  const items = [item({ key: 'REV-DONE', state: 'done', last: '2026-07-01 09:00' })];
  assert.deepEqual(staleItems(items, { workdays: 5, now: at('2026-08-21') }), []);
});

test('a blocked item going silent is the worst case, not an exempt one', () => {
  const items = [item({ key: 'REV-BLOCKED', state: 'blocked', last: '2026-08-14 09:00' })];
  const found = staleItems(items, { workdays: 5, now: at('2026-08-21') });
  assert.equal(found.length, 1, '"blocked" is a claim somebody made a week ago and nobody revisited');
  assert.equal(found[0]?.workdays, 5);
});

test('an unreadable date is skipped rather than treated as ancient', () => {
  const items = [item({ key: 'REV-BAD', last: 'ไม่รู้' })];
  assert.deepEqual(staleItems(items, { workdays: 5, now: at('2026-08-21') }), []);
});

test('the longest silence is reported first', () => {
  const items = [
    item({ key: 'REV-NEW', last: '2026-08-17 09:00' }),
    item({ key: 'REV-OLD', last: '2026-08-03 09:00' }),
  ];
  const found = staleItems(items, { workdays: 3, now: at('2026-08-21') });
  assert.deepEqual(found.map((f) => f.item.key), ['REV-OLD', 'REV-NEW']);
});

test('the announcement key repeats at each multiple of the threshold, not every morning', () => {
  const one = { item: item(), since: '', workdays: 5 };
  const two = { item: item(), since: '', workdays: 7 };
  const three = { item: item(), since: '', workdays: 10 };
  assert.equal(staleKey(one, 5), staleKey(two, 5), 'day 7 is the same escalation as day 5, already said');
  assert.notEqual(staleKey(one, 5), staleKey(three, 5), 'day 10 is worth saying again, with a bigger number');
});
