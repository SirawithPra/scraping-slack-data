/**
 * Where the bot is allowed to leave a visible message.
 *
 * This runs against a real company workspace with four private work channels the bot
 * is a member of. The instruction is narrow — post only to the designated test channel
 * and to one person's DM — and an instruction that lives only in a config value is one
 * `chat.postMessage` away from being violated by a well-meaning edit. These tests pin
 * the behaviour instead: that the guard sits on the client rather than on nineteen call
 * sites, that an unlisted channel is refused rather than logged, and that a missing
 * configuration means silence rather than a default of "anywhere".
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { describePolicy, guardPosting, readPolicy, PostRefused } from '../src/postguard.js';

/** A stand-in client that records what actually reached Slack. */
function spy() {
  const sent: string[] = [];
  const client = { chat: { postMessage: async (a: { channel?: string }) => { sent.push(String(a.channel ?? '')); return { ok: true }; } } };
  return { client: client as never, sent };
}

test('an unlisted channel is refused, and nothing reaches Slack', async () => {
  const { client, sent } = spy();
  guardPosting(client, readPolicy({ DIGEST_CHANNEL: 'C0TEST0001', STANDUP_USERS: 'U0PERSON01' } as never));
  const chat = (client as unknown as { chat: { postMessage: (a: unknown) => Promise<unknown> } }).chat;

  await assert.rejects(() => chat.postMessage({ channel: 'C0WORKCHAN', text: 'x' }), PostRefused);
  assert.deepEqual(sent, [], 'a refused post must not reach the client underneath');
});

test('the two configured targets are allowed', async () => {
  const { client, sent } = spy();
  guardPosting(client, readPolicy({ DIGEST_CHANNEL: 'C0TEST0001', STANDUP_USERS: 'U0PERSON01' } as never));
  const chat = (client as unknown as { chat: { postMessage: (a: unknown) => Promise<unknown> } }).chat;

  await chat.postMessage({ channel: 'C0TEST0001', text: 'x' });
  await chat.postMessage({ channel: 'U0PERSON01', text: 'x' });
  assert.deepEqual(sent, ['C0TEST0001', 'U0PERSON01']);
});

test('no configuration means nothing may be posted, not everything', async () => {
  // The dangerous default. A fresh clone with an empty .env should be silent; a bot
  // that treats "unconfigured" as "unrestricted" is loud in someone else's channel.
  const { client, sent } = spy();
  guardPosting(client, readPolicy({} as never));
  const chat = (client as unknown as { chat: { postMessage: (a: unknown) => Promise<unknown> } }).chat;

  await assert.rejects(() => chat.postMessage({ channel: 'C0ANYTHING', text: 'x' }), PostRefused);
  assert.deepEqual(sent, []);
});

test('a post with no channel at all is refused', async () => {
  const { client, sent } = spy();
  guardPosting(client, readPolicy({ DIGEST_CHANNEL: 'C0TEST0001' } as never));
  const chat = (client as unknown as { chat: { postMessage: (a: unknown) => Promise<unknown> } }).chat;
  await assert.rejects(() => chat.postMessage({ text: 'x' }), PostRefused);
  assert.deepEqual(sent, []);
});

test('trailing comments beside an id do not disable the allowlist', () => {
  // This .env really does carry notes: `DIGEST_CHANNEL=C0… #bot-testing — test channel`.
  // dotenv keeps an inline comment unless a space precedes the '#', so the value can
  // arrive with the note attached. Splitting on whitespace would then produce an entry
  // that matches no channel — and an allowlist that matches nothing either refuses
  // everything (visible) or, if someone "fixes" it by falling open, permits everything.
  const p = readPolicy({ DIGEST_CHANNEL: 'C0TEST0001 #bot-testing — ช่องทดสอบ', STANDUP_USERS: 'U0PERSON01 #me' } as never);
  assert.deepEqual([...p.allowed], ['C0TEST0001', 'U0PERSON01']);
});

test('SLACK_POST_ALLOWLIST overrides, so a deliberate widening is explicit', () => {
  const p = readPolicy({ SLACK_POST_ALLOWLIST: 'C0AAAAAAA,C0BBBBBBB', DIGEST_CHANNEL: 'C0TEST0001' } as never);
  assert.deepEqual([...p.allowed], ['C0AAAAAAA', 'C0BBBBBBB']);
  assert.match(p.source, /SLACK_POST_ALLOWLIST/);
});

test('guarding twice does not stack the check or lose the original', async () => {
  // app.ts guards at boot; a future refactor calling it again per-listener must be safe.
  const { client, sent } = spy();
  const policy = readPolicy({ DIGEST_CHANNEL: 'C0TEST0001' } as never);
  guardPosting(client, policy);
  guardPosting(client, policy);
  const chat = (client as unknown as { chat: { postMessage: (a: unknown) => Promise<unknown> } }).chat;
  await chat.postMessage({ channel: 'C0TEST0001', text: 'x' });
  assert.deepEqual(sent, ['C0TEST0001'], 'the real client must still be called exactly once');
});

test('the boot banner names the targets, so the blast radius is visible before traffic', () => {
  const line = describePolicy(readPolicy({ DIGEST_CHANNEL: 'C0TEST0001', STANDUP_USERS: 'U0PERSON01' } as never));
  assert.match(line, /C0TEST0001/);
  assert.match(line, /U0PERSON01/);
  assert.match(describePolicy(readPolicy({} as never)), /ไม่มีที่ไหนเลย/);
});
