/**
 * `/meowtam format` — the shape the analysis can actually read.
 *
 * This exists because of a measurement, not a hunch. On the team's own export the
 * pipeline found 22 occurrences of the word "blockers" and 14 of them were the
 * template's own question; of the three places somebody described a real obstacle,
 * only one contained any word the blocked_by cue list looks for. The other two were
 * ordinary sentences about waiting on another team.
 *
 * So the highest-leverage change is not a longer keyword list — it is telling people
 * where to type, because text under "Are there any blockers?" needs no inference at
 * all. Teaching that inside Slack, next to where the typing happens, beats putting it
 * in a document nobody opens at 09:25.
 *
 * The copy names what each field buys the reader, rather than just listing fields: a
 * form whose purpose is unexplained gets filled in with "-".
 */

import type { KnownBlock } from '@slack/types';

import { context, section } from './common.js';

/** The form, exactly as the parser reads it. Kept in one place so the two agree. */
export const STANDUP_FORM = [
  '• What did accomplish yesterday?',
  'ปิด ticket sorting แล้ว รอ QA',
  '• What are you working on today?',
  'ต่อหน้า profile',
  '• Are there any blockers?',
  'รอ requirement จาก ROPS ก่อน ถึงจะปรับ UI ต่อได้',
  '• Others',
  '-',
].join('\n');

export function formatBlocks(): KnownBlock[] {
  return [
    section(
      '*พิมพ์แบบนี้ แล้วระบบอ่านได้เลย ไม่ต้องเดา*\n' +
        'ข้อความที่คุณพิมพ์ใต้หัวข้อ _Are there any blockers?_ ถือเป็น blocker ' +
        'ตามที่*คุณบอกเอง* — ไม่ต้องพึ่งการเดาจากคำในประโยค',
    ),
    section('```' + STANDUP_FORM + '```'),
    section(
      '*ทำไมสำคัญ*\n' +
        'จาก 3 ครั้งที่มีคนเขียน blocker จริงในช่องนี้ ระบบเดาจากคำได้แค่ *1 ครั้ง* — ' +
        'อีก 2 ครั้งเป็นประโยคธรรมดาอย่าง “รอ requirement จากอีกทีม” ที่ไม่มีคำไหนให้จับเลย\n' +
        'กรอกช่องนี้ = ระบบไม่ต้องเดา และหลักฐานคือข้อความของคุณเอง',
    ),
    section(
      '*สองอย่างที่ช่วยได้อีก*\n' +
        '• *ไม่มี blocker ก็เขียนว่า `-` หรือ `None`* — “ตอบว่าไม่มี” ต่างจาก “ไม่ได้ตอบ” ' +
        'และระบบเชื่อคำตอบนั้น\n' +
        '• *ใส่รหัส ticket ถ้ามี* เช่น `PROJ-142` — ระบบจะใช้เป็นชื่องานเอง ' +
        'ทำให้งานเดียวกันไม่แตกเป็นหลายชิ้น',
    ),
    section(
      '*บันทึกประชุม*\n' +
        'ส่งออกไฟล์ `.vtt` หรือ `.srt` จาก Zoom / Meet / Teams แล้วลากใส่หน้า `/upload` ' +
        'ของ dashboard — บทประชุมจะไปอยู่ในงานชิ้นเดียวกับที่คุยใน Slack ไม่ใช่ไฟล์แยก',
    ),
    context('เขียนอิสระได้เหมือนเดิม — form แค่ทำให้ระบบไม่ต้องเดา · `/meowtam format` ดูอันนี้ซ้ำได้ตลอด'),
  ];
}
