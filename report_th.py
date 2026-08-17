"""รายงานผลแบบอ่านง่าย ภาษาไทย -> output/report_th.html

    python3 report_th.py
    python3 report_th.py --records data/processed/demo_messages.json \
                         --eval-file data/eval_queries.demo.json

ต่างจาก visualize.py ที่รายงานเป็นตัวเลข metric ไฟล์นี้เล่าเป็นภาษาคน:
แทนที่จะบอก "Recall@10 = 0.70" จะบอกว่า "เจอ 7 จาก 10 ข้อความที่ควรเจอ"
"""

from __future__ import annotations

import argparse
import html
import logging
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from dotenv import load_dotenv

from embeddings import cosine_scores, embed_texts, model_name, quiet_third_party_logs, set_model
from evaluate import DEFAULT_EVAL_FILE, load_eval_set
from semantic_search import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from visualize import (
    DE_EMPHASIS,
    INK_MUTED,
    INK_SECONDARY,
    SERIES_1,
    SURFACE,
    base_layout,
    build_page,
    shorten,
    stat_tile,
    wrap_hover,
)

DEFAULT_OUTPUT = Path("output/report_th.html")

log = logging.getLogger("report_th")


def score_case(
    query: str, relevant: set[str], record_ids: list[str], matrix: np.ndarray, top_k: int
) -> dict[str, Any]:
    """ผลของคำถามเดียว: เจอกี่ข้อความจากที่ควรเจอ และอันดับแรกถูกไหม"""
    scores = cosine_scores(embed_texts([query], role="query")[0], matrix)
    order = np.argsort(-scores)
    ranked_ids = [record_ids[index] for index in order]
    top_ids = ranked_ids[:top_k]
    return {
        "query": query,
        "relevant": relevant,
        "found": len(relevant.intersection(top_ids)),
        "total": len(relevant),
        "reachable": min(top_k, len(relevant)),  # เพดาน: แสดงแค่ top_k อันดับ
        "first_correct": ranked_ids[0] in relevant,
        "scores": scores,
        "order": order,
    }


def found_per_query_figure(results: list[dict[str, Any]], top_k: int) -> go.Figure:
    """แท่งเดียวต่อคำถาม: ส่วนน้ำเงินคือเจอ ส่วนเทาคือพลาด (part-to-whole)"""
    labels = [
        f"{shorten(result['query'], 40)}<br><span style='color:{INK_MUTED}'>"
        f"ควรเจอ {result['total']} ข้อความ</span>"
        for result in results
    ]
    found = [result["found"] for result in results]
    missed = [result["total"] - result["found"] for result in results]

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=found,
            y=labels,
            orientation="h",
            name="เจอ",
            marker={"color": SERIES_1, "line": {"width": 2, "color": SURFACE}},
            text=[f"{value}" for value in found],
            textposition="inside",
            insidetextfont={"color": SURFACE},
            hovertemplate="%{y}<br>เจอ %{x} ข้อความ<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            x=missed,
            y=labels,
            orientation="h",
            name="ไม่เจอ",
            marker={"color": DE_EMPHASIS, "line": {"width": 2, "color": SURFACE}},
            text=[f"{value}" if value else "" for value in missed],
            textposition="inside",
            insidetextfont={"color": INK_SECONDARY},
            hovertemplate="%{y}<br>ไม่เจอ %{x} ข้อความ<extra></extra>",
        )
    )
    layout = base_layout(f"แต่ละคำถาม เจอข้อความที่เกี่ยวข้องกี่ข้อความ (ดู {top_k} อันดับแรก)", height=150 + 74 * len(results))
    layout["margin"] |= {"l": 300}
    layout["barmode"] = "stack"
    layout["showlegend"] = True
    # plotly flips legend order for stacked bars; keep it matching the bar order.
    layout["legend"] = {"orientation": "h", "y": -0.18, "x": 0, "traceorder": "normal", "font": {"color": INK_SECONDARY}}
    layout["xaxis"] |= {"title": {"text": "จำนวนข้อความ", "font": {"color": INK_MUTED}}, "dtick": 1}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE, "categoryorder": "array", "categoryarray": labels}
    figure.update_layout(**layout)
    return figure


def example_figure(
    result: dict[str, Any], records: list[dict[str, Any]], record_ids: list[str], top_k: int
) -> tuple[go.Figure, list[tuple[int, float, dict[str, Any], bool]]]:
    """ผลค้นหาจริงของคำถามหนึ่ง ทำเครื่องหมายว่าอันไหนตรงตามที่ label ไว้"""
    rows: list[tuple[int, float, dict[str, Any], bool]] = []
    for position, index in enumerate(result["order"][:top_k], start=1):
        is_relevant = record_ids[index] in result["relevant"]
        rows.append((position, float(result["scores"][index]), records[index], is_relevant))

    labels = [
        f"{position}. {'✓' if is_relevant else '○'} {shorten(str(record['text']), 52)}"
        for position, _, record, is_relevant in rows
    ]
    figure = go.Figure()
    for name, color, wanted in (("ตรงตามที่ label ไว้", SERIES_1, True), ("ไม่ได้ label ไว้", DE_EMPHASIS, False)):
        picked = [(label, row) for label, row in zip(labels, rows) if row[3] is wanted]
        if not picked:
            continue
        figure.add_trace(
            go.Bar(
                x=[row[1] for _, row in picked],
                y=[label for label, _ in picked],
                orientation="h",
                name=name,
                marker={"color": color, "cornerradius": 4},
                text=[f"{row[1]:.2f}" for _, row in picked],
                textposition="outside",
                textfont={"color": INK_SECONDARY},
                customdata=[[wrap_hover(str(row[2]["text"])), row[2].get("user") or "-"] for _, row in picked],
                hovertemplate="คะแนน %{x:.2f}<br>%{customdata[0]}<br><i>%{customdata[1]}</i><extra></extra>",
            )
        )
    layout = base_layout(f"ตัวอย่างผลค้นหาจริง: “{shorten(result['query'], 50)}”", height=130 + 42 * len(rows))
    layout["margin"] |= {"l": 400}
    layout["xaxis"] |= {"range": [0, 1], "title": {"text": "คะแนนความใกล้เคียง 0–1 (cosine)", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE, "categoryorder": "array", "categoryarray": labels}
    layout["showlegend"] = True
    layout["legend"] = {"orientation": "h", "y": -0.12, "x": 0, "font": {"color": INK_SECONDARY}}
    layout["bargap"] = 0.35
    figure.update_layout(**layout)
    return figure, rows


def depth_figure(results: list[dict[str, Any]], record_ids: list[str], ks: tuple[int, ...]) -> go.Figure:
    """ยิ่งดูลึกกี่อันดับ ยิ่งเจอมากขึ้นเท่าไหร่ (นับรวมทุกคำถาม)"""
    totals = []
    for k in ks:
        found = 0
        for result in results:
            top_ids = [record_ids[index] for index in result["order"][:k]]
            found += len(result["relevant"].intersection(top_ids))
        totals.append(found)
    overall = sum(result["total"] for result in results)

    figure = go.Figure(
        go.Bar(
            x=[f"{k} อันดับแรก" for k in ks],
            y=totals,
            marker={"color": SERIES_1, "cornerradius": 4},
            text=[f"{value}/{overall}" for value in totals],
            textposition="outside",
            textfont={"color": INK_SECONDARY},
            hovertemplate="ดู %{x}<br>เจอ %{y} จาก " + str(overall) + " ข้อความ<extra></extra>",
        )
    )
    # ไม่ใส่ชื่อแกน Y เพราะภาษาไทยแนวตั้งอ่านยาก ใส่ไว้ในหัวข้อกราฟแทน
    layout = base_layout(f"ถ้าดูผลลึกขึ้น จะเจอข้อความที่เกี่ยวข้องมากขึ้น (จากทั้งหมด {overall} ข้อความ)", height=400)
    layout["yaxis"] |= {"range": [0, overall * 1.15]}
    layout["bargap"] = 0.5
    figure.update_layout(**layout)
    return figure


def results_table(rows: list[tuple[int, float, dict[str, Any], bool]]) -> str:
    body = "".join(
        "<tr>"
        f"<td class='num'>{position}</td>"
        f"<td class='num'>{score:.2f}</td>"
        f"<td>{'✓ ตรง' if is_relevant else '○'}</td>"
        f"<td>{html.escape(' '.join(str(record['text']).split())[:180])}</td>"
        f"<td>{html.escape(str(record.get('user') or '-'))}</td>"
        f"<td>{html.escape(format_timestamp(str(record.get('ts', ''))))}</td>"
        "</tr>"
        for position, score, record, is_relevant in rows
    )
    return (
        "<details class='table-view'><summary>ดูเป็นตาราง (อ่านข้อความเต็ม)</summary>"
        "<table><thead><tr><th>อันดับ</th><th>คะแนน</th><th>ตรงไหม</th><th>ข้อความ</th>"
        f"<th>คนพูด</th><th>เวลา</th></tr></thead><tbody>{body}</tbody></table></details>"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help="ไฟล์ records ที่เตรียมไว้")
    parser.add_argument("--eval-file", type=Path, default=DEFAULT_EVAL_FILE, help="ไฟล์คำถามที่ label ไว้")
    parser.add_argument("--top-k", type=int, default=10, help="ดูกี่อันดับแรก (ค่าเริ่มต้น 10)")
    parser.add_argument("--query", help="คำถามที่จะใช้เป็นตัวอย่าง (ค่าเริ่มต้น: คำถามแรกในไฟล์ label)")
    parser.add_argument("--model", help="Embedding model id")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"ไฟล์ผลลัพธ์ (ค่าเริ่มต้น {DEFAULT_OUTPUT})")
    parser.add_argument("--include-threads", action="store_true", help="รวม thread record ด้วย")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)
    if args.top_k <= 0:
        raise SystemExit("--top-k ต้องมากกว่าศูนย์")

    records = load_records(args.records, include_threads=args.include_threads)
    record_ids = [str(record["id"]) for record in records]
    known = set(record_ids)
    matrix = embed_records(records)

    cases = load_eval_set(args.eval_file)
    results: list[dict[str, Any]] = []
    for case in cases:
        relevant = {str(value) for value in case["relevant_ids"]}.intersection(known)
        if not relevant:
            log.warning("ข้าม %r เพราะ id ที่ label ไว้ไม่มีใน corpus", shorten(str(case["query"]), 40))
            continue
        results.append(score_case(str(case["query"]), relevant, record_ids, matrix, args.top_k))
    if not results:
        raise SystemExit("ไม่มีคำถามไหนที่ใช้ได้ ตรวจ id ในไฟล์ label ว่าตรงกับ records หรือไม่")

    total_found = sum(result["found"] for result in results)
    total_relevant = sum(result["total"] for result in results)
    reachable = sum(result["reachable"] for result in results)
    first_correct = sum(1 for result in results if result["first_correct"])

    example = next((r for r in results if r["query"] == args.query), results[0]) if args.query else results[0]
    example_fig, example_rows = example_figure(example, records, record_ids, args.top_k)

    tiles = [
        stat_tile("ข้อความที่ค้นได้", f"{len(records)}", "ข้อความจาก Slack ที่เตรียมไว้แล้ว"),
        stat_tile("คำถามที่ทดสอบ", f"{len(results)}", "label ด้วยมือว่าข้อความไหนควรเจอ"),
        stat_tile("อันดับ 1 ถูกต้อง", f"{first_correct} / {len(results)}", "คำถามที่ผลอันดับแรกเกี่ยวข้องจริง"),
        stat_tile(
            f"เจอใน {args.top_k} อันดับแรก",
            f"{total_found} / {total_relevant}",
            f"คิดเป็น {total_found / total_relevant:.0%} ของข้อความที่ควรเจอ",
        ),
    ]

    verdict = (
        f"ระบบค้นหาด้วยความหมายทำงานได้: จาก {len(results)} คำถามที่ทดสอบ "
        f"ผลอันดับ 1 เกี่ยวข้องจริง {first_correct} คำถาม "
        f"และเมื่อดู {args.top_k} อันดับแรก เจอข้อความที่ควรเจอ {total_found} จาก {total_relevant} ข้อความ "
        f"โดยไม่มีการแปลภาษาใด ๆ ในระบบ — query ภาษาไทยหาข้อความอังกฤษเจอ และกลับกันด้วย"
    )

    sections = [
        (
            verdict,
            example_fig.to_html(full_html=False, include_plotlyjs=True, config={"displayModeBar": False})
            + results_table(example_rows),
        ),
        (
            "แท่งน้ำเงินคือข้อความที่ระบบหาเจอ แท่งเทาคือข้อความที่เกี่ยวข้องแต่ระบบหาไม่เจอ "
            f"ยิ่งน้ำเงินเต็มแท่ง ยิ่งดี หมายเหตุ: ถ้าคำถามไหน label ไว้มากกว่า {args.top_k} ข้อความ "
            f"ยังไงก็เจอได้ไม่เกิน {args.top_k} เพราะเราดูแค่ {args.top_k} อันดับแรก",
            found_per_query_figure(results, args.top_k).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
        (
            "ถ้ายอมดูผลลึกขึ้น จะเจอข้อความที่เกี่ยวข้องมากขึ้นเรื่อย ๆ ใช้ตัดสินใจได้ว่าหน้าจอจริง "
            "ควรโชว์กี่อันดับ",
            depth_figure(results, record_ids, (1, 3, 5, args.top_k)).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
    ]

    subtitle = (
        f"ทดสอบกับข้อความ Slack จริง {len(records)} ข้อความ · {len(results)} คำถาม · "
        f"model {model_name().split('/')[-1]} · เจอสูงสุดที่เป็นไปได้ {reachable}/{total_relevant} ข้อความ"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page("ผลทดสอบ ค้นหาข้อความ Slack ด้วยความหมาย", tiles, sections, subtitle), encoding="utf-8")
    log.info("เขียน %s แล้ว เปิดด้วย: open %s", args.out, args.out)


if __name__ == "__main__":
    main()
