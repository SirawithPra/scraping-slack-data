/**
 * What a ticket comment is allowed to be.
 *
 * These pin the failure that prompted the module. What the bot wrote on a real ticket
 * was two lines long and, read cold a week later, unanswerable:
 *
 *     แนบบทสนทนาจาก Slack: “การ deploy ขึ้น sit” (2026-08-18) — 3 ข้อความ
 *     เก็บเข้า corpus ของ Meowtam แล้ว โดย U08H0UD5R36 เมื่อ 2026-08-20 08:08
 *
 * Who is U08H0UD5R36. What did the three messages say. Where is the corpus. Was this a
 * message from a channel with a permalink or a chat somebody pasted by hand — because
 * the answer changes how far to trust the quote. Each test below is one of those
 * questions, asked of the text that now goes onto the ticket.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { driftComment, linkComment, pasteComment, person, sharableUrl } from '../src/comment.js';
import { fromMarkdown } from '../src/blocks/common.js';
import { commentAnchor } from '../src/youtrack.js';
import { resetNames } from '../src/names.js';

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'meowtam-comment-'));
  process.env.TAM_NAMES_PATH = join(dir, 'user_names.json');
  writeFileSync(process.env.TAM_NAMES_PATH, JSON.stringify({ U08H0UD5R36: 'Aim Sirawith', U0NATTAJAH: 'jah natta' }));
  process.env.TAM_NAMES = 'slack';
  resetNames();
});

afterEach(() => {
  delete process.env.TAM_NAMES;
  delete process.env.TAM_NAMES_PATH;
  resetNames();
});

const CHAT = [
  { user: 'U08H0UD5R36', when: '2026-08-18 14:21', text: 'พี่ครับ หน้า redemption สรุปว่าใช้ voucher code เดิมใช่ไหมครับ' },
  { user: 'U0NATTAJAH', when: '2026-08-18 14:24', text: 'ใช้เดิมไปก่อนนะ\nPO ยังไม่ยืนยัน format ใหม่' },
  { user: 'U08H0UD5R36', when: '2026-08-18 14:26', text: 'โอเคครับ' },
];

const paste = (over: Record<string, unknown> = {}) =>
  pasteComment({
    key: 'REVERAPP-251',
    title: 'การ deploy ขึ้น sit',
    day: '2026-08-18',
    by: 'U08H0UD5R36',
    at: '2026-08-20 08:08',
    records: CHAT,
    itemKey: 'REVERAPP-251',
    inItem: 3,
    boardUrl: 'https://tam.example.com/item/REVERAPP-251',
    ...(over as any),
  });

test('a raw Slack id never stands alone as the name of who did this', () => {
  const body = paste();
  assert.ok(body.includes('Aim Sirawith'), 'the reader has to be able to recognise a colleague');
  assert.ok(body.includes('U08H0UD5R36'), 'the id stays too — it is the only half that is searchable');
});

test('the comment says the messages came from a chat with no permalink behind it', () => {
  const body = paste();
  assert.ok(body.includes('แชทปิด'), 'the provenance is the thing a reader weighs the quote by');
  assert.ok(body.includes('slack_paste'), 'and the stored type is named, so the two screens can be matched up');
  assert.ok(
    body.includes('token ของบอทเข้าไม่ถึง'),
    'why there is no link to click is the question the reader asks next',
  );
});

test('the conversation itself is on the ticket, not a count of it', () => {
  const body = paste();
  assert.ok(body.includes('voucher code เดิม'), 'attaching three messages and quoting none of them says nothing');
  assert.ok(body.includes('PO ยังไม่ยืนยัน format ใหม่'), 'a multi-line message keeps its lines');
  assert.ok(body.includes('> **jah natta** · 2026-08-18 14:24'), 'each quote is attributed and stamped');
});

test('a long chat is trimmed and says how much it trimmed', () => {
  const many = Array.from({ length: 20 }, (_, i) => ({
    user: 'U0NATTAJAH', when: `2026-08-18 15:${String(i).padStart(2, '0')}`, text: `บรรทัดที่ ${i}`,
  }));
  const body = paste({ records: many });
  assert.ok(body.includes('อีก 8 ข้อความไม่ได้ยกมา'), 'silent truncation reads as a complete transcript');
  assert.ok(!body.includes('บรรทัดที่ 15'), 'past the cap is genuinely not quoted');
});

test('a localhost board link is left off the ticket entirely', () => {
  /**
   * The board url is built from TAM_API_URL, which on the machine that runs the demo is
   * http://localhost:8000. A Slack button pointing there is fine — the presenter is at
   * that laptop. On a ticket other people read it is a dead link with a long life.
   */
  assert.equal(sharableUrl('http://localhost:8000/item/REVERAPP-251'), undefined);
  assert.equal(sharableUrl('http://127.0.0.1:8000/item/X'), undefined);
  assert.equal(sharableUrl('http://192.168.1.40:8000/item/X'), undefined);
  assert.equal(sharableUrl('https://tam.example.com/item/X'), 'https://tam.example.com/item/X');

  const local = paste({ boardUrl: sharableUrl('http://localhost:8000/item/REVERAPP-251') });
  assert.ok(!local.includes('localhost'), 'a dead link on a real ticket is worse than one fewer line');
  assert.ok(paste().includes('https://tam.example.com/item/REVERAPP-251'), 'a reachable one is offered');
});

test('the comment states it changed nothing else about the ticket', () => {
  assert.ok(paste().includes('ไม่ได้แก้ description'), 'the reader should not have to check');
});

test('a linked message leads with the reason the person gave for linking it', () => {
  const body = linkComment({
    key: 'REVERAPP-140',
    by: 'U0NATTAJAH',
    at: '2026-08-20 09:10',
    note: 'อันนี้คือที่ PO ยืนยันว่าใช้ format เดิม',
    messages: 1,
    source: 'slack',
    records: [CHAT[1]!],
    permalink: 'https://acme.slack.com/archives/C0AAA/p1755500000000100',
    boardUrl: 'https://tam.example.com/item/REVERAPP-140',
  });
  assert.ok(body.indexOf('อันนี้คือที่ PO ยืนยัน') < body.indexOf('**ที่มา:**'), 'their sentence is the reason; it leads');
  assert.ok(body.includes('[เปิดข้อความนี้ใน Slack](https://acme.slack.com/archives/'), 'a two-way link is the point of writing at all');
  assert.ok(body.includes('ห้องที่บอทอ่านได้เอง'), 'this one does have a permalink, and says so');
  assert.ok(!body.includes('แชทปิด'), 'and must not borrow the pasted-chat caveat');
});

test('a drift proposal says on the ticket that it is a proposal', () => {
  const body = driftComment({
    key: 'REVERAPP-251',
    by: 'U0NATTAJAH',
    at: '2026-08-20 09:30',
    note: 'ticket ยังเขียนว่า voucher format ใหม่',
    scope: 'ใช้ voucher code เดิม\nformat ใหม่แยกเป็น ticket ใหม่',
    boardUrl: 'https://tam.example.com/item/REVERAPP-251',
  });
  assert.ok(body.includes('ยังไม่ได้แก้ description ให้'), 'overwriting a PO’s description has no undo');
  assert.ok(body.includes('> ใช้ voucher code เดิม'), 'the proposed scope is quoted, line by line');
  assert.ok(body.includes('jah natta'), 'and attributed to a person, not an id');
});

test('person() degrades to the bare id rather than inventing a name', () => {
  process.env.TAM_NAMES = 'id';
  resetNames();
  assert.equal(person('U08H0UD5R36'), '`U08H0UD5R36`');
  assert.equal(person(''), 'ไม่รู้ว่าใคร');
});

test('the comment id is enough to link straight to the comment', () => {
  /** YouTrack anchors a comment this way; without it the reader is told an id and left to scroll. */
  assert.equal(
    commentAnchor('https://yt.example.com/issue/REVERAPP-251', '7-729'),
    'https://yt.example.com/issue/REVERAPP-251#focus=Comments-7-729.0-0',
  );
  assert.equal(commentAnchor('https://yt.example.com/issue/X', ''), 'https://yt.example.com/issue/X');
  assert.equal(commentAnchor('', '7-729'), '');
});

test('the comment renders back into Slack as text, not as raw Markdown', () => {
  const shown = fromMarkdown(paste(), 4000);
  assert.ok(!shown.includes('###'), 'a heading marker on screen is a different way of being unreadable');
  assert.ok(!shown.includes('**'), 'Slack bold is one asterisk');
  assert.ok(shown.includes('*📋 แนบบทสนทนาจาก Slack'), 'the heading survives as bold');
  assert.ok(shown.includes('•  '), 'the fact list stays a list');
  assert.ok(
    shown.includes('<https://tam.example.com/item/REVERAPP-251|เปิดงาน REVERAPP-251 ในบอร์ด Meowtam>'),
    'a Markdown link becomes a Slack link, not visible bracket soup',
  );
  assert.ok(!/^-{3,}$/m.test(shown), 'Slack has no horizontal rule; the marker would show through');
  assert.ok(!shown.includes('&gt; *'), 'the quoted chat renders as a Slack quote, not as escaped angle brackets');
  assert.ok(shown.includes('> *jah natta*'), 'and the attribution keeps its quote marker');
});

test("Slack's own wire syntax is unwrapped before it reaches the ticket", () => {
  /**
   * A message body from the API is not what somebody typed. Quoting it raw put
   * `<@U08H0UD5R36>` on the ticket — the same unreadable id, moved from the byline into
   * the quote — plus bracket noise around every link and channel.
   */
  const body = paste({
    records: [{
      user: 'U0NATTAJAH',
      when: '2026-08-18 14:24',
      text: 'ถาม <@U08H0UD5R36> ใน <#C0ABCDEF|dev-be> ดู · สเปคอยู่ <https://x.test/spec|ที่นี่>',
    }],
  });
  assert.ok(body.includes('@Aim Sirawith'), 'a mention is a person, not an id in brackets');
  assert.ok(body.includes('#dev-be') && !body.includes('C0ABCDEF'), 'a channel reference is its name');
  assert.ok(body.includes('ที่นี่ (https://x.test/spec)'), 'a link keeps both its words and its url');
  assert.ok(!body.includes('<@') && !body.includes('<#'), 'none of the wire syntax survives');
});

test('a stray angle bracket in a chat log stays text in the Slack preview', () => {
  const shown = fromMarkdown(
    pasteComment({
      key: 'X-1', title: 't', day: '2026-08-18', by: 'U0NATTAJAH', at: 'now',
      records: [{ user: 'U0NATTAJAH', when: 'w', text: 'เช็ค if (retry < 3) ก่อน' }],
    }),
    4000,
  );
  assert.ok(shown.includes('retry &lt; 3'), 'text a person typed must not become markup on the way to a preview');
});
