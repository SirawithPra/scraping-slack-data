/**
 * The tracked artefact under slack-bot/data/ must stay publishable.
 *
 * `npm run ledger` writes `data/ledger.json` from a real Slack export — verbatim
 * message text, user ids, channel names, permalinks. That file used to be the
 * *tracked* one, so the documented workflow left real content staged in a public
 * repo and one `git commit -a` from publication. The generated file is now never
 * the tracked one: `data/ledger.fixture.json` is the committed demo ledger and is
 * written by nothing.
 *
 * A note in a README does not survive a hurried afternoon, so the arrangement is
 * asserted here instead: the generated path is ignored, the tracked fixture is
 * the only file git knows about under data/, and its contents stay inside the
 * demo namespace.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const BOT = fileURLToPath(new URL('..', import.meta.url));
const FIXTURE = `${BOT}data/ledger.fixture.json`;

/**
 * Hostnames this file is allowed to name. An allowlist, not a list of banned
 * tenants: naming the tenant here would publish it in the very test that exists to
 * keep it unpublished, and a blocklist only ever knows about the leak it was
 * written after.
 */
const ALLOWED_HOSTS = /^(example|localhost|127\.0\.0\.1|synthetic)\b/;

function git(...args: string[]): string {
  return execFileSync('git', args, { cwd: BOT, encoding: 'utf8' }).trim();
}

test('the committed fixture names no real workspace or ticket host', () => {
  const text = readFileSync(FIXTURE, 'utf8');
  const links = text.match(/https?:\/\/[^"\s]+/g) ?? [];
  assert.ok(links.length, 'no URLs found — the regex stopped matching, so this test proves nothing');
  for (const link of links) {
    const host = new URL(link).hostname;
    assert.match(host, ALLOWED_HOSTS, `${host} is not a placeholder host — real tenant in ${FIXTURE}?`);
  }
});

test('every Slack id in the committed fixture is in the demo namespace', () => {
  const text = readFileSync(FIXTURE, 'utf8');
  const ids = new Set(text.match(/\b[CU][A-Z0-9_]{6,}\b/g) ?? []);
  assert.ok(ids.size, 'no ids found — the regex stopped matching, so this test proves nothing');
  for (const id of ids) {
    assert.match(id, /^(C0DEMO|U0DEMO|U_DEMO_)/, `${id} is not a demo id — real workspace content?`);
  }
});

test('the file `npm run ledger` writes is ignored, and nothing under data/ is tracked but the fixture', () => {
  // check-ignore exits 1 when the path is *not* ignored, which execFileSync throws on.
  assert.doesNotThrow(() => git('check-ignore', '-q', 'data/ledger.json'),
    'data/ledger.json is not ignored — `npm run ledger` can be committed');

  const tracked = git('ls-files', 'data').split('\n').filter(Boolean);
  // Positive first. Without this the suite passes while *nothing* is tracked under
  // data/ — the half-committed state where the old ledger's deletion has landed and
  // the replacement fixture has not, which breaks `npm run seed` on a fresh clone
  // and so breaks start, preview and this suite itself.
  assert.ok(tracked.includes('data/ledger.fixture.json'),
    'data/ledger.fixture.json is not tracked — a clone gets nothing to seed from');

  const strays = tracked.filter((p) => p !== 'data/ledger.fixture.json');
  assert.deepEqual(strays, [],
    `only the demo fixture may be tracked under slack-bot/data/, found: ${strays.join(', ')}`);

  // …and the fixture itself must not be ignored, or a fresh clone has nothing to
  // seed from and `npm start` cannot run offline.
  assert.throws(() => git('check-ignore', '-q', 'data/ledger.fixture.json'),
    'data/ledger.fixture.json is ignored — it would never reach a clone');
});

test('the bot loads a path that exists after `npm run seed`', () => {
  // src/data.ts reads data/ledger.json; the seed step copies the fixture there on
  // a fresh clone, so the offline demo runs with no Slack access exactly as before.
  const ledger = JSON.parse(readFileSync(`${BOT}data/ledger.json`, 'utf8'));
  assert.ok(Array.isArray(ledger.items) && ledger.items.length, 'the seeded ledger has no work items');
});
