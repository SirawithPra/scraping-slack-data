/**
 * The seeded morning, which is the one part of this bot that writes data that is not true.
 *
 * That makes its tests different in kind from the rest. They are not only checking that
 * the demo works — they are checking that it cannot be mistaken for the real thing and
 * that it can be taken back out completely. A seeded morning that survives `demo clear`,
 * or a streak counted from seeded days that does not say so, is the failure this file
 * exists to catch: a screenshot of three mornings that never happened, indistinguishable
 * from three that did.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { pendingStreaks, newEscalations, streakKey } from '../src/daily.js';
import { clearSimulated, previousWorkdays, seedPendingStreak, todaysSimulatedAnswers, isSimulatedTs } from '../src/demo.js';
import { readDailies, saveDaily } from '../src/store.js';
import { dailySummaryBlocks } from '../src/blocks/daily.js';

/** Each test gets its own dailies file: these functions write, and the real one is data. */
function withStore<T>(fn: () => T): T {
  const dir = mkdtempSync(join(tmpdir(), 'meowtam-demo-'));
  const before = process.env.TAM_DAILIES_PATH;
  process.env.TAM_DAILIES_PATH = join(dir, 'dailies.json');
  try {
    return fn();
  } finally {
    if (before === undefined) delete process.env.TAM_DAILIES_PATH;
    else process.env.TAM_DAILIES_PATH = before;
    rmSync(dir, { recursive: true, force: true });
  }
}

test('the seeded mornings are working days, never a weekend that had no standup', () => {
  // 2026-08-24 is a Monday; the two mornings before it are the Friday and the Thursday.
  assert.deepEqual(previousWorkdays('2026-08-24', 2), ['2026-08-20', '2026-08-21']);
});

test('a seeded run plus today crosses the threshold, and the streak says it was seeded', () => {
  withStore(() => {
    const today = '2026-08-24';
    const users = ['U0AAA', 'U0BBB'];
    const dates = seedPendingStreak({ today, channel: 'C0X', users, mornings: 2 });
    assert.equal(dates.length, 2);

    saveDaily({
      date: today,
      channel: 'C0X',
      ts: '1787.1',
      posted_at: `${today} 09:00`,
      answers: todaysSimulatedAnswers(users, today),
    });

    const streaks = pendingStreaks(readDailies());
    const recurring = streaks.find((s) => s.user === 'U0AAA');
    assert.ok(recurring, 'the person whose blocker repeats must appear');
    assert.equal(recurring.days, 3, 'two seeded mornings plus today is the third');
    assert.equal(recurring.since, dates[0], 'the run starts at the oldest seeded morning');
    assert.equal(recurring.simulated, true, 'a count built on seeded days must say so');

    // The blocker is written three different ways on purpose — this asserts the
    // normaliser is what joins them, not string equality.
    const wordings = new Set(
      readDailies().flatMap((d) => d.answers.filter((a) => a.user === 'U0AAA').flatMap((a) => a.blockers.map((b) => b.text))),
    );
    assert.ok(wordings.size > 1, 'identical strings would demonstrate nothing');

    const stuck = newEscalations(readDailies(), streaks, 3);
    assert.ok(stuck.some((s) => streakKey(s) === streakKey(recurring)), 'the escalation must actually fire');
  });
});

test('every seeded answer is flagged, and its ts is recognisable as having no message behind it', () => {
  withStore(() => {
    seedPendingStreak({ today: '2026-08-24', channel: 'C0X', users: ['U0AAA'], mornings: 2 });
    for (const record of readDailies()) {
      assert.equal(record.simulated, true);
      for (const answer of record.answers) {
        assert.equal(answer.simulated, true);
        assert.ok(isSimulatedTs(answer.ts), 'a permalink built from this would be a dead link');
      }
    }
  });
});

test('clear takes the seeded mornings out and leaves the real ones alone', () => {
  withStore(() => {
    const today = '2026-08-24';
    seedPendingStreak({ today, channel: 'C0X', users: ['U0AAA'], mornings: 2 });
    // A real morning that also picked up seeded answers: the day survives, the
    // seeded answers do not.
    saveDaily({
      date: today,
      channel: 'C0X',
      ts: '1787.1',
      posted_at: `${today} 09:00`,
      answers: [
        { user: 'U0REAL', ts: '1787.2', done: ['ของจริง'], focus: [], blockers: [] },
        ...todaysSimulatedAnswers(['U0AAA'], today),
      ],
    });

    const removed = clearSimulated();
    assert.equal(removed.days, 2);
    assert.ok(removed.answers >= 1);

    const left = readDailies();
    assert.equal(left.length, 1, 'only the real morning remains');
    assert.equal(left[0]?.date, today);
    assert.deepEqual(left[0]?.answers.map((a) => a.user), ['U0REAL']);
    assert.equal(left[0]?.simulated, undefined, 'a day with nothing seeded left in it is not a seeded day');
  });
});

test('clear is idempotent, so running it twice cannot eat real data', () => {
  withStore(() => {
    saveDaily({ date: '2026-08-24', channel: 'C0X', ts: '1.1', posted_at: '', answers: [] });
    clearSimulated();
    const after = clearSimulated();
    assert.deepEqual(after, { days: 0, answers: 0 });
    assert.equal(readDailies().length, 1);
  });
});

/**
 * The label switch, both ways.
 *
 * `DEMO_SHOW_SIMULATED` governs one thing — whether the room is told a row was
 * seeded. What it must never touch is the flag on the record itself, because that
 * is what `demo clear` keys on: a demo that cannot be removed is the demo becoming
 * the data. So this checks the label moves and the flag does not.
 */
test('the simulated label is off by default and comes back when asked for', () => {
  const before = process.env.DEMO_SHOW_SIMULATED;
  const record = {
    date: '2026-08-20',
    channel: 'C0DEMO',
    ts: '1787.1',
    posted_at: '2026-08-20 09:00',
    answers: todaysSimulatedAnswers(['U0DEMOUSER1'], '2026-08-20'),
  } as any;
  const render = () =>
    JSON.stringify(
      dailySummaryBlocks({ record, expected: ['U0DEMOUSER1'], unfilled: [], streaks: [], pendingDays: 3 }),
    );

  try {
    delete process.env.DEMO_SHOW_SIMULATED;
    const hidden = render();
    assert.ok(!hidden.includes('จำลอง'), 'ไม่ควรมีป้ายจำลองเมื่อปิดสวิตช์');

    process.env.DEMO_SHOW_SIMULATED = '1';
    const shown = render();
    assert.ok(shown.includes('(จำลอง)'), 'ควรมีป้ายจำลองเมื่อเปิดสวิตช์');
  } finally {
    if (before === undefined) delete process.env.DEMO_SHOW_SIMULATED;
    else process.env.DEMO_SHOW_SIMULATED = before;
  }

  // Either way the record is still marked, so `demo clear` can still find it.
  assert.ok(record.answers.every((a: any) => a.simulated));
});

/**
 * The seeded wordings must all be one streak.
 *
 * The demo's claim is that the counter matches the obstacle rather than the string,
 * so it varies the wording every morning — and `normaliseBlocker` absorbs decoration
 * (a leading "Pending", punctuation, mentions, case) but not an extra word. A variant
 * that steps outside that splits three mornings into separate runs and the escalation
 * never fires, which is only visible when TAM_DAILY_PENDING_DAYS is raised past the
 * two mornings a default demo seeds.
 */
test('every wording of the recurring blocker counts as the same one', () => {
  const dir = mkdtempSync(join(tmpdir(), 'meowtam-recurring-'));
  process.env.TAM_DAILIES_PATH = join(dir, 'dailies.json');
  try {
    const users = ['U0DEMOUSER1', 'U0DEMOUSER2'];
    // Four mornings walks the whole RECURRING list, which two never reaches.
    seedPendingStreak({ today: '2026-08-21', channel: 'C0DEMO', users, mornings: 4 });
    const keys = new Set(
      readDailies().flatMap((d) => d.answers.filter((a) => a.user === users[0]).flatMap((a) => a.blockers.map((b) => streakKey({ user: a.user, text: b.text })))),
    );
    assert.equal(keys.size, 1, `${keys.size} คีย์ — ทุกสำนวนต้องนับเป็นเรื่องเดียวกัน`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    delete process.env.TAM_DAILIES_PATH;
  }
});
