"""Render the search and evaluation results as an HTML report.

    python3 -m tam.report.visualize
    python3 -m tam.report.visualize --query "แอป Android ล่มตอนกดเข้าหน้าโปรไฟล์" --top-k 10

Writes output/report.html -- open it in a browser. Embeddings come from the
same cache the search uses, so this recomputes nothing.
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

from tam.retrieval.embeddings import cosine_scores, embed_texts, model_name, quiet_third_party_logs, set_model
from tam.evaluation.evaluate import DEFAULT_EVAL_FILE, first_hit_rank, load_eval_set, recall_at_k
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records

DEFAULT_OUTPUT = Path("output/report.html")
DEFAULT_KS = (1, 3, 5, 10)

# Chart chrome and ink, and the same palette the deck uses — one project, one look.
#
# The school is security-infra (Evervault et al.): a committed dark ground, hairlines at
# exactly 1px, monospace on every label and every number, and one warm accent used
# sparingly enough that it always means something. The accent is the paw the bot has
# signed its Slack messages with since it was written, so the brand mark and the accent
# are the same thing rather than two decisions.
#
# Dark is the default, not a preference. Light exists under [data-theme="light"] for one
# practical reason: a projector in a bright room eats a dark page alive.
SURFACE = "#141110"
PAGE = "#0C0A09"          # warm near-black; pure #000 goes cold beside coral
INK = "#F5EFEC"
INK_SECONDARY = "#A99C97"
INK_MUTED = "#948781"
GRID = "#241F1D"          # the hairline
AXIS = "#332C29"
SERIES_1 = "#F0866A"      # the paw
SERIES_2 = "#6FBFA4"      # the one contrast, for "verified" rather than a second brand hue
# Sequential coral ramp, dark -> light, so it reads on the dark ground. One hue, never a rainbow.
BLUE_SCALE = [[0.0, "#3A1D15"], [0.25, "#6E3527"], [0.5, "#A9503A"], [0.75, "#D97256"], [1.0, "#F5A992"]]
# No webfont: a CSP blocks font CDNs on published pages and the content is Thai, so a
# missing face would fall back silently and lose the script's shaping.
FONT = '"Noto Sans Thai", "Sukhumvit Set", system-ui, -apple-system, "Segoe UI", sans-serif'
MONO = 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace'
DE_EMPHASIS = "#332C29"   # neutral for "absence"; never a second hue
WARN = "#E0A860"
GOOD = "#6FBFA4"
ACCENT_DIM = "#8E4634"
ON_ACCENT = "#1A0F0B"

# The light escape hatch, same values as the deck's [data-theme="light"] block.
LIGHT = {
    "page": "#FBF7F4", "surface": "#FFFDFC", "ink": "#241C1A", "ink2": "#5E4E49",
    "ink3": "#756259", "grid": "#EADFD9", "accent": "#BF5138", "accent2": "#2F6455",
    "warn": "#95610F", "on_accent": "#FFFDFC", "accent_dim": "#E4735A",
}

log = logging.getLogger("visualize")


def base_layout(title: str, height: int = 420) -> dict[str, Any]:
    """Shared layout: recessive grid and axes, ink-coloured text, no chart junk."""
    return {
        "title": {"text": title, "font": {"size": 15, "color": INK, "family": FONT}, "x": 0, "xanchor": "left"},
        "height": height,
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT, "size": 12, "color": INK_SECONDARY},
        "margin": {"l": 60, "r": 24, "t": 48, "b": 48},
        "xaxis": {"gridcolor": GRID, "linecolor": AXIS, "zerolinecolor": AXIS, "tickfont": {"color": INK_MUTED}},
        "yaxis": {"gridcolor": GRID, "linecolor": AXIS, "zerolinecolor": AXIS, "tickfont": {"color": INK_MUTED}},
        "showlegend": False,
        "hoverlabel": {"font": {"family": FONT}},
    }


def shorten(text: str, limit: int) -> str:
    single = " ".join(text.split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def wrap_hover(text: str, width: int = 60) -> str:
    """Break long message text so the tooltip stays a readable column."""
    words, lines, current = " ".join(text.split()).split(" "), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return "<br>".join(line for line in lines if line)


def top_matches_figure(
    query: str, records: list[dict[str, Any]], scores: np.ndarray, top_k: int
) -> tuple[go.Figure, list[tuple[float, dict[str, Any]]]]:
    """Ranked matches for one query. One series, so one colour for every bar."""
    order = np.argsort(-scores)[:top_k]
    matches = [(float(scores[index]), records[index]) for index in order]
    labels = [f"{position}. {shorten(str(record['text']), 58)}" for position, (_, record) in enumerate(matches, 1)]
    figure = go.Figure(
        go.Bar(
            x=[score for score, _ in matches],
            y=labels,
            orientation="h",
            marker={"color": SERIES_1, "cornerradius": 4},
            text=[f"{score:.2f}" for score, _ in matches],
            textposition="outside",
            textfont={"color": INK_SECONDARY},
            customdata=[
                [wrap_hover(str(record["text"])), record.get("user") or "-", format_timestamp(str(record.get("ts", "")))]
                for _, record in matches
            ],
            hovertemplate="<b>%{x:.3f}</b> cosine<br>%{customdata[0]}<br><i>%{customdata[1]} · %{customdata[2]}</i><extra></extra>",
        )
    )
    layout = base_layout(f"Top {len(matches)} matches for “{shorten(query, 60)}”", height=60 + 42 * len(matches))
    layout["xaxis"] |= {"range": [0, 1], "title": {"text": "cosine similarity", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    layout["margin"] |= {"l": 430}
    layout["bargap"] = 0.35
    figure.update_layout(**layout)
    return figure, matches


def recall_heatmap(
    cases: list[dict[str, Any]], records: list[dict[str, Any]], matrix: np.ndarray, ks: tuple[int, ...]
) -> tuple[go.Figure | None, dict[int, float], list[int]]:
    """Recall per query per K as a grid: magnitude across a grid is a heatmap."""
    record_ids = [str(record["id"]) for record in records]
    known = set(record_ids)
    rows, labels, ranks = [], [], []
    totals = {k: 0.0 for k in ks}

    for case in cases:
        relevant = {str(value) for value in case["relevant_ids"]}.intersection(known)
        if not relevant:
            log.warning("Skipping %r: no labelled id is in the corpus", shorten(str(case["query"]), 40))
            continue
        scores = cosine_scores(embed_texts([str(case["query"])], role="query")[0], matrix)
        ranked = [record_ids[index] for index in np.argsort(-scores)]
        row = [recall_at_k(ranked, relevant, k) for k in ks]
        for k, value in zip(ks, row):
            totals[k] += value
        rows.append(row)
        labels.append(f"{shorten(str(case['query']), 42)}<br><span style='color:{INK_MUTED}'>{len(relevant)} labelled</span>")
        rank = first_hit_rank(ranked, relevant)
        ranks.append(rank if rank else len(record_ids))

    if not rows:
        return None, {k: 0.0 for k in ks}, []

    figure = go.Figure(
        go.Heatmap(
            z=rows,
            x=[f"top {k}" for k in ks],
            y=labels,
            zmin=0,
            zmax=1,
            colorscale=BLUE_SCALE,
            xgap=2,
            ygap=2,
            texttemplate="%{z:.2f}",
            textfont={"size": 13, "family": FONT},
            colorbar={"title": {"text": "recall", "font": {"color": INK_MUTED}}, "outlinewidth": 0, "tickfont": {"color": INK_MUTED}},
            hovertemplate="%{y}<br>%{x}: recall %{z:.2f}<extra></extra>",
        )
    )
    layout = base_layout("Recall by query — share of labelled messages found in the top K", height=140 + 78 * len(rows))
    layout["margin"] |= {"l": 300}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    figure.update_layout(**layout)
    return figure, {k: totals[k] / len(rows) for k in ks}, ranks


def pca_2d(matrix: np.ndarray, query_vector: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project records and the query onto the first two principal components."""
    mean = matrix.mean(axis=0)
    centered = matrix - mean
    _, singular, components = np.linalg.svd(centered, full_matrices=False)
    with np.errstate(all="ignore"):  # Accelerate BLAS flag noise; see embeddings.cosine_scores
        coords = centered @ components[:2].T
        query_coords = (query_vector - mean) @ components[:2].T
    if not (np.isfinite(coords).all() and np.isfinite(query_coords).all()):
        raise ValueError("PCA produced non-finite coordinates.")
    variance = singular**2
    return coords, query_coords, variance[:2] / variance.sum()


def map_figure(
    query: str, records: list[dict[str, Any]], matrix: np.ndarray, scores: np.ndarray, query_vector: np.ndarray
) -> go.Figure:
    """Every message as a point, coloured by similarity to the query."""
    coords, query_coords, explained = pca_2d(matrix, query_vector)
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=coords[:, 0],
            y=coords[:, 1],
            mode="markers",
            marker={
                "size": 13,
                "color": scores,
                "colorscale": BLUE_SCALE,
                "cmin": float(scores.min()),
                "cmax": float(scores.max()),
                "line": {"width": 2, "color": SURFACE},  # 2px surface ring on overlap
                "colorbar": {"title": {"text": "cosine to<br>query", "font": {"color": INK_MUTED}}, "outlinewidth": 0, "tickfont": {"color": INK_MUTED}},
            },
            customdata=[
                [wrap_hover(str(record["text"])), record.get("user") or "-", f"{float(score):.3f}"]
                for record, score in zip(records, scores)
            ],
            hovertemplate="<b>%{customdata[2]}</b> cosine<br>%{customdata[0]}<br><i>%{customdata[1]}</i><extra></extra>",
            name="messages",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[float(query_coords[0])],
            y=[float(query_coords[1])],
            mode="markers+text",
            marker={"size": 17, "color": SERIES_2, "symbol": "diamond", "line": {"width": 2, "color": SURFACE}},
            text=["your query"],
            textposition="top center",
            textfont={"color": SERIES_2, "size": 12, "family": FONT},
            hovertemplate=f"<b>your query</b><br>{wrap_hover(query)}<extra></extra>",
            name="query",
        )
    )
    layout = base_layout("Message map — nearby points mean similar meaning", height=520)
    layout["xaxis"] |= {"title": {"text": f"component 1 ({explained[0]:.0%} of variance)", "font": {"color": INK_MUTED}}, "zeroline": False}
    layout["yaxis"] |= {"title": {"text": f"component 2 ({explained[1]:.0%})", "font": {"color": INK_MUTED}}, "zeroline": False}
    figure.update_layout(**layout)
    return figure


def thread_similarity_figure(records: list[dict[str, Any]], matrix: np.ndarray) -> go.Figure | None:
    """Do same-conversation pairs really score higher than unrelated pairs?"""
    threads = np.array([str(record.get("thread_ts", "")) for record in records])
    similarity = np.clip(cosine_scores(matrix.T, matrix), -1.0, 1.0)
    upper = np.triu_indices(len(records), k=1)
    same_thread = threads[upper[0]] == threads[upper[1]]
    same, other = similarity[upper][same_thread], similarity[upper][~same_thread]
    if not len(same) or not len(other):
        return None

    figure = go.Figure()
    for values, color, name in ((other, SERIES_1, "different threads"), (same, SERIES_2, "same thread")):
        figure.add_trace(
            go.Histogram(
                x=values,
                name=f"{name} (n={len(values)}, median {np.median(values):.2f})",
                marker={"color": color, "line": {"width": 2, "color": SURFACE}},
                opacity=0.75,
                histnorm="percent",
                xbins={"start": -0.2, "end": 1.0, "size": 0.05},
                hovertemplate="cosine %{x}<br>%{y:.1f}% of pairs<extra>%{fullData.name}</extra>",
            )
        )
    layout = base_layout("Does similarity track real topics? Pairs of messages, by cosine score", height=420)
    layout["xaxis"] |= {"title": {"text": "cosine similarity between two messages", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"title": {"text": "% of pairs", "font": {"color": INK_MUTED}}}
    layout["barmode"] = "overlay"
    layout["showlegend"] = True
    layout["legend"] = {"orientation": "h", "y": -0.22, "x": 0, "font": {"color": INK_SECONDARY}}
    figure.update_layout(**layout)
    return figure


def stat_tile(label: str, value: str, note: str) -> str:
    return (
        f'<div class="tile"><div class="tile-label">{html.escape(label)}</div>'
        f'<div class="tile-value">{html.escape(value)}</div>'
        f'<div class="tile-note">{html.escape(note)}</div></div>'
    )


def matches_table(matches: list[tuple[float, dict[str, Any]]]) -> str:
    """Table view of the ranked matches, so nothing depends on colour alone."""
    rows = "".join(
        "<tr>"
        f"<td class='num'>{position}</td><td class='num'>{score:.3f}</td>"
        f"<td>{html.escape(' '.join(str(record['text']).split())[:200])}</td>"
        f"<td>{html.escape(str(record.get('user') or '-'))}</td>"
        f"<td>{html.escape(format_timestamp(str(record.get('ts', ''))))}</td>"
        "</tr>"
        for position, (score, record) in enumerate(matches, start=1)
    )
    return (
        "<details class='table-view'><summary>Show the same matches as a table</summary>"
        "<table><thead><tr><th>#</th><th>cosine</th><th>message</th><th>user</th><th>time</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></details>"
    )


def build_page(title: str, tiles: list[str], sections: list[tuple[str, str]], subtitle: str | None = None) -> str:
    body = "".join(
        f'<section><p class="lede">{html.escape(note)}</p>'
        f'<div class="plot-wrap">{figure_html}</div></section>'
        for note, figure_html in sections
    )
    return f"""<!doctype html>
<html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  /* Dark is the design, so the bare :root carries it and nothing is left to
     prefers-color-scheme. Light lives behind an explicit stamp, for a bright room. */
  :root {{
    --page:{PAGE}; --surface:{SURFACE}; --surface-2:#1B1716;
    --ink:{INK}; --ink2:{INK_SECONDARY}; --ink3:{INK_MUTED};
    --line:{GRID}; --line-2:{AXIS};
    --accent:{SERIES_1}; --accent-dim:{ACCENT_DIM}; --accent-wash:rgba(240,134,106,.10);
    --good:{GOOD}; --warn:{WARN}; --on-accent:{ON_ACCENT};
    --u:4px; --r:3px;
    --f-mono:{MONO};
    --paw:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cellipse cx='12' cy='16' rx='6.2' ry='5.1' fill='%23000'/%3E%3Ccircle cx='5.2' cy='9.2' r='2.6' fill='%23000'/%3E%3Ccircle cx='9.8' cy='6.1' r='2.75' fill='%23000'/%3E%3Ccircle cx='14.2' cy='6.1' r='2.75' fill='%23000'/%3E%3Ccircle cx='18.8' cy='9.2' r='2.6' fill='%23000'/%3E%3C/svg%3E");
  }}
  :root[data-theme="light"] {{
    --page:{LIGHT["page"]}; --surface:{LIGHT["surface"]}; --surface-2:#F5EDE8;
    --ink:{LIGHT["ink"]}; --ink2:{LIGHT["ink2"]}; --ink3:{LIGHT["ink3"]};
    --line:{LIGHT["grid"]}; --line-2:#D3C2BA;
    --accent:{LIGHT["accent"]}; --accent-dim:{LIGHT["accent_dim"]}; --accent-wash:rgba(191,81,56,.08);
    --good:{LIGHT["accent2"]}; --warn:{LIGHT["warn"]}; --on-accent:{LIGHT["on_accent"]};
  }}

  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: calc(var(--u)*8) calc(var(--u)*5) calc(var(--u)*18);
         background: var(--page); color: var(--ink);
         font-family: {FONT}; font-size: .93rem; line-height: 1.68;
         -webkit-font-smoothing: antialiased; }}
  main {{ max-width: 1100px; margin: 0 auto; }}

  /* The paw carries the heading: it is the product's mark, not an ornament. */
  h1 {{ font-size: 1.7rem; line-height: 1.22; margin: 0 0 calc(var(--u)*2);
        letter-spacing: -.022em; font-weight: 650; text-wrap: balance;
        display: flex; align-items: center; gap: calc(var(--u)*3); }}
  h1::before {{ content: ""; flex: none; width: 22px; height: 22px; background: var(--accent);
        -webkit-mask: var(--paw) center/contain no-repeat; mask: var(--paw) center/contain no-repeat; }}
  .sub {{ font-family: var(--f-mono); font-size: .7rem; text-transform: uppercase;
          letter-spacing: .11em; color: var(--ink3); margin: 0 0 calc(var(--u)*7); }}

  .tiles {{ display: grid; gap: calc(var(--u)*5); margin-bottom: calc(var(--u)*7);
            grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); }}
  /* A metric is the loudest thing on the page: mono, tabular, and sat on a rule rather
     than boxed in a card. Boxes around numbers add chrome and say nothing. */
  .tile {{ background: transparent; border: 0; border-top: 1px solid var(--line-2);
           border-radius: 0; padding: calc(var(--u)*4) 0 0; }}
  .tile-label {{ font-family: var(--f-mono); font-size: .68rem; color: var(--ink3);
                 text-transform: uppercase; letter-spacing: .11em; }}
  .tile-value {{ font-family: var(--f-mono); font-size: 2.1rem; font-weight: 600; color: var(--ink);
                 margin: calc(var(--u)*2) 0 0; font-variant-numeric: tabular-nums; letter-spacing: -.035em; }}
  .tile-note {{ font-size: .7rem; color: var(--ink3); margin-top: calc(var(--u)*1.5); }}

  section {{ background: var(--surface); border: 1px solid var(--line); border-radius: var(--r);
             padding: calc(var(--u)*6) calc(var(--u)*6) calc(var(--u)*3); margin-bottom: calc(var(--u)*5); }}
  .lede {{ margin: 0 0 calc(var(--u)*2); font-size: .82rem; color: var(--ink2); }}
  .table-view {{ margin: calc(var(--u)*1) 0 calc(var(--u)*4); font-size: .82rem; }}
  .table-view summary {{ cursor: pointer; color: var(--accent); }}
  table {{ border-collapse: collapse; width: 100%; margin-top: calc(var(--u)*3); font-size: .82rem; }}
  th, td {{ text-align: left; padding: calc(var(--u)*2.5) calc(var(--u)*3);
            border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ font-family: var(--f-mono); font-size: .68rem; color: var(--ink3); font-weight: 600;
        text-transform: uppercase; letter-spacing: .11em; white-space: nowrap; }}
  td.num {{ font-family: var(--f-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  /* Wide content scrolls in its own box so the page body never moves sideways. */
  .plot-wrap {{ overflow-x: auto; }}
  a {{ color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-dim); }}
  a:hover {{ border-bottom-color: var(--accent); }}
  code {{ font-family: var(--f-mono); font-size: .9em; background: var(--surface-2);
          border: 1px solid var(--line); border-radius: 2px; padding: .05em .34em; }}
  :focus-visible {{ outline: 1px solid var(--accent); outline-offset: 3px; }}
  ::selection {{ background: var(--accent); color: var(--on-accent); }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">{html.escape(subtitle or f"Model {model_name()} · cosine similarity · no translation anywhere in the pipeline")}</p>
<div class="tiles">{''.join(tiles)}</div>
{body}
</main></body></html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", help="Query to chart; defaults to the first labelled eval query")
    parser.add_argument("--top-k", type=int, default=10, help="Matches to chart (default 10)")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--eval-file", type=Path, default=DEFAULT_EVAL_FILE, help=f"Labelled queries (default {DEFAULT_EVAL_FILE})")
    parser.add_argument("--ks", type=int, nargs="+", default=list(DEFAULT_KS), help="K values (default 1 3 5 10)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output HTML (default {DEFAULT_OUTPUT})")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--include-threads", action="store_true", help="Also chart whole-thread records")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)
    ks = tuple(sorted({k for k in args.ks if k > 0}))
    if not ks:
        raise SystemExit("--ks needs at least one positive integer.")
    if args.top_k <= 0:
        # Without this the empty slice reaches matches[0] and dies on IndexError
        # several hundred lines later, after the whole corpus has been embedded.
        raise SystemExit("--top-k must be greater than zero.")

    records = load_records(args.records, include_threads=args.include_threads)
    matrix = embed_records(records)

    cases = load_eval_set(args.eval_file) if args.eval_file.exists() else []
    if not cases:
        log.warning("No %s, so the recall chart is skipped.", args.eval_file)
    query = args.query or (str(cases[0]["query"]) if cases else "")
    if not query:
        raise SystemExit("Pass --query, or create a label file so a query can be taken from it.")

    log.info("Charting %d record(s) and %d labelled query/queries", len(records), len(cases))
    query_vector = embed_texts([query], role="query")[0]
    scores = cosine_scores(query_vector, matrix)

    sections: list[tuple[str, str]] = []
    plotly_js: bool | str = True  # inline the bundle once, so the file works offline

    matches_fig, matches = top_matches_figure(query, records, scores, args.top_k)
    sections.append(
        (
            "Every bar is one historical Slack message. Longer means closer in meaning to the query — "
            "not shared keywords. Hover a bar for the full message.",
            matches_fig.to_html(full_html=False, include_plotlyjs=plotly_js, config={"displayModeBar": False})
            + matches_table(matches),
        )
    )
    plotly_js = False

    means: dict[int, float] = {}
    ranks: list[int] = []
    if cases:
        recall_fig, means, ranks = recall_heatmap(cases, records, matrix, ks)
        if recall_fig is not None:
            sections.append(
                (
                    "Each row is a hand-labelled query; each column is how deep we look. "
                    "1.00 means every message marked relevant was found. Recall@1 cannot beat "
                    "1 / number-of-labelled-messages, so read the wider columns.",
                    recall_fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
                )
            )

    sections.append(
        (
            "The 384-dimension embeddings flattened to two dimensions. Points that sit together are "
            "about the same thing; the orange diamond is the query, so its neighbours are the answer. "
            "Read it as a sketch, not evidence — the axis labels show how little of the variation "
            "survives the flattening, and the ranking above uses all 384 dimensions.",
            map_figure(query, records, matrix, scores, query_vector).to_html(
                full_html=False, include_plotlyjs=False, config={"displayModeBar": False}
            ),
        )
    )

    thread_fig = thread_similarity_figure(records, matrix)
    if thread_fig is not None:
        sections.append(
            (
                "The sanity check. Messages from the same Slack thread are the same topic by definition. "
                "If the orange distribution sits to the right of the blue one, cosine similarity is "
                "tracking real topical relatedness rather than noise.",
                thread_fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
            )
        )

    tiles = [stat_tile("Messages searched", f"{len(records)}", "prepared Slack records")]
    for k in ks:
        if k in means:
            tiles.append(stat_tile(f"Recall@{k}", f"{means[k]:.2f}", f"mean over {len(ranks)} labelled queries"))
    if ranks:
        tiles.append(stat_tile("First hit rank", f"{max(ranks)}", "worst case across labelled queries"))
    tiles.append(stat_tile("Top match", f"{matches[0][0]:.2f}", "cosine for the charted query"))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page("Slack Semantic Search — results", tiles, sections), encoding="utf-8")
    log.info("Wrote %s (%.1f MB). Open it with: open %s", args.out, args.out.stat().st_size / 1e6, args.out)


if __name__ == "__main__":
    main()
