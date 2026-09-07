"""
Rendering results as something a person can read and a reader can check.

The comparison table is the output that matters, and its shape is the argument:
every backend on the same rows, so nobody has to take on faith that they were
run the same way.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

KS = (1, 3, 5, 10)


def _pct(value) -> str:
    return "--" if value is None else f"{value * 100:.1f}"


def comparison_table(summaries: dict[str, dict]) -> str:
    """One row per backend, one column per k, plus QA accuracy and cost."""
    header = ("| Backend | " + " | ".join(f"R@{k}" for k in KS)
              + " | QA acc | mean ingest (s) | mean query (s) |")
    divider = "|---" * (len(KS) + 4) + "|"

    rows = []
    for name, summary in summaries.items():
        recalls = " | ".join(_pct(summary["recall_at_k"].get(k)) for k in KS)
        rows.append(
            f"| `{name}` | {recalls} | {_pct(summary['qa_accuracy'])} | "
            f"{summary['mean_ingest_seconds']:.2f} | "
            f"{summary['mean_query_seconds']:.3f} |"
        )
    return "\n".join([header, divider, *rows])


def per_type_table(summaries: dict[str, dict], k: int = 5) -> str:
    """
    Recall@k broken out by question type.

    Worth reading before the headline number: knowledge-update and abstention
    questions are where a memory system's handling of superseded facts shows
    up, and an average hides that entirely.
    """
    types = sorted({t for s in summaries.values() for t in s["by_question_type"]})
    if not types:
        return "_No per-type breakdown available._"

    header = f"| Question type | n | " + " | ".join(f"`{n}`" for n in summaries) + " |"
    divider = "|---" * (len(summaries) + 2) + "|"

    rows = []
    for qtype in types:
        counts = [s["by_question_type"].get(qtype, {}).get("n", 0)
                  for s in summaries.values()]
        cells = []
        for summary in summaries.values():
            entry = summary["by_question_type"].get(qtype, {})
            cells.append(_pct((entry.get("recall_at_k") or {}).get(k)))
        rows.append(f"| {qtype} | {max(counts) if counts else 0} | "
                    + " | ".join(cells) + " |")

    return "\n".join([f"**Recall@{k} by question type**", "", header, divider, *rows])


def write_report(out_dir: Path, summaries: dict[str, dict], meta: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    (out_dir / f"results_{stamp}.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries}, indent=2),
        encoding="utf-8")

    md = out_dir / f"report_{stamp}.md"
    lines = [
        "# LongMemEval — MMU and baselines",
        "",
        f"Run {datetime.now(timezone.utc).isoformat()}",
        "",
        "| | |",
        "|---|---|",
        f"| Dataset | `{meta.get('dataset')}` |",
        f"| Instances | {meta.get('n_instances')} |",
        f"| top_k | {meta.get('top_k')} |",
        f"| Embedding model | `{meta.get('embedding_model')}` |",
        f"| Reader model | `{meta.get('reader_model') or 'not run'}` |",
        f"| Judge model | `{meta.get('judge_model') or 'not run'}` |",
        "",
        "## Results",
        "",
        comparison_table(summaries),
        "",
        "**R@k** is retrieval recall: the share of questions where at least one of the",
        "top *k* retrieved items came from a session that actually contained the answer.",
        "No model is involved, so this number is exactly reproducible.",
        "",
        "**QA acc** depends on the reader and judge models named above. A stronger reader",
        "lifts every row at once, so this column compares backends only within this table,",
        "never across runs with different models.",
        "",
        per_type_table(summaries),
        "",
        "## How to read this",
        "",
        "The row that matters is `flat_vector`: the same embeddings, the same haystack,",
        "no graph. The gap between `mmu` and `flat_vector` is what MMU's graph layer is",
        "worth on this benchmark. If there is no gap, there is no gap, and that is the",
        "finding.",
        "",
        "`bm25` is the lexical floor. Anything that fails to clear it is not doing",
        "semantic retrieval usefully, whatever else it is doing.",
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    return md
