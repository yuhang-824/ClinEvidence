"""ClinEvidence 评测汇总:从 judge/answered 产物生成多版本对比报告(markdown)。

输入显式传参,缺文件即报错退出(不静默写 None,不覆盖既有报告)。
用法:
  python scripts/eval_final_report.py --retrieval <retrieval_summary.json> \
      --variant 基线=<answered.jsonl>,<scores.jsonl> \
      --variant v1=<answered.jsonl>,<scores.jsonl> ... --out <report.md>
uncertainty 标记读取 answered 行自带的 uncertainty_sensitive 字段。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

HONEST_PATTERN = re.compile(
    r"未(见|曾|记载|提及|描述|发现)|没有(记载|提及|描述)|病历(中)?未见|未提供|无法(确定|判断)|未回报|不得作为该患者的诊疗决策"
)


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"缺少输入文件: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_scores(path: Path) -> dict[int, dict]:
    return {
        row["row_number"]: row
        for row in load_jsonl(path)
        if isinstance(row.get("score"), int)
    }


def variant_stats(answered: list[dict], scores: dict[int, dict]) -> dict:
    values = [s["score"] for s in scores.values()]
    done = [r for r in answered if r.get("status") == "completed"]
    zero_source = sum(
        1 for r in done if not ((r.get("sources") or {}).get("patient") or (r.get("sources") or {}).get("knowledge"))
    )
    uncertain = [r for r in done if r.get("uncertainty_sensitive")]
    honest = sum(1 for r in uncertain if HONEST_PATTERN.search(r.get("response") or ""))
    uncertain_scores = [scores[r["row_number"]]["score"] for r in uncertain if r["row_number"] in scores]
    return {
        "completed": len(done),
        "zero_source_rate": round(zero_source / len(done) * 100, 1) if done else None,
        "mean": round(statistics.mean(values), 2) if values else None,
        "low_rate": round(sum(1 for v in values if v <= 2) / len(values) * 100, 1) if values else None,
        "uncertain_mean": round(statistics.mean(uncertain_scores), 2) if uncertain_scores else None,
        "uncertain_honest_rate": round(honest / len(uncertain) * 100, 1) if uncertain else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval", required=True)
    parser.add_argument(
        "--variant",
        action="append",
        required=True,
        help="标签=<answered.jsonl>,<scores.jsonl>,可重复",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    retrieval_path = Path(args.retrieval)
    if not retrieval_path.is_file():
        raise SystemExit(f"缺少输入文件: {retrieval_path}")
    retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))

    variants: dict[str, dict] = {}
    for spec in args.variant:
        label, _, pair = spec.partition("=")
        answered_path, _, scores_path = pair.partition(",")
        answered = load_jsonl(Path(answered_path))
        scores = load_scores(Path(scores_path))
        variants[label] = variant_stats(answered, scores)

    lines = ["# ClinEvidence 评测对比", ""]
    p, k = retrieval.get("patient_channel", {}), retrieval.get("kb_channel", {})
    lines += [
        "## 检索层",
        "",
        f"- 患者通道(n={p.get('n')}):Recall@5 {p.get('recall@5')}% / Recall@10 {p.get('recall@10')}% / MRR {p.get('mrr')}",
        f"- 知识通道(n={k.get('n')}):Recall@5 {k.get('recall@5')}% / Recall@10 {k.get('recall@10')}% / MRR {k.get('mrr')}",
        "",
        "## 生成层(逐题 rubric,LLM judge)",
        "",
        "| 指标 | " + " | ".join(variants) + " |",
        "|---|" + "---|" * len(variants),
    ]
    fields = ["completed", "mean", "low_rate", "zero_source_rate", "uncertain_mean", "uncertain_honest_rate"]
    labels = {
        "completed": "完成题数",
        "mean": "RAGAS 均分",
        "low_rate": "低分率 % (≤2)",
        "zero_source_rate": "零来源回答率 %",
        "uncertain_mean": "敏感题均分",
        "uncertain_honest_rate": "敏感题显式缺失声明率 %",
    }
    for field in fields:
        lines.append("| " + labels[field] + " | " + " | ".join(str(v[field]) for v in variants.values()) + " |")
    lines.append("")

    out_path = Path(args.out)
    if out_path.exists():
        raise SystemExit(f"输出文件已存在,拒绝覆盖: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
