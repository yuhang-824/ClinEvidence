"""ClinEvidence RAGAS 评分 judge:MiniMax-M3 按逐题 rubric 打 1-5 分。

数据契约沿用 RAGAS SingleTurnSample(user_input/response/reference/rubric);
因 rubric 为逐题定制,评分用直连 judge 调用而非 ragas 内置 metric 类,口径在报告如实声明。
输入:harness 产出的 answered jsonl;输出:逐题得分 jsonl + 汇总 json。
用法:uv run --with httpx python scripts/eval_ragas_judge.py --answered ... --input ... --output-prefix ...
环境:EVAL_MINIMAX_BASE_URL / EVAL_MINIMAX_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import httpx

JUDGE_MODEL = "MiniMax-M3"
JUDGE_TIMEOUT = 180.0

SYSTEM_PROMPT = """你是临床问答系统的严格评审。依据参考答案与评分 rubric,对被测系统回答打 1-5 分。
只依据 rubric 描述评分;被测回答声称"病历未记载/未见记载"而参考答案确有内容时,按 rubric 的 score1/score2 口径判低。
只输出 JSON:{"score": <1-5 整数>, "reason": "<50字内依据>"}"""

HONEST_PATTERN = re.compile(r"未(见|曾|记载|提及|描述|发现)|没有(记载|提及|描述)|病历(中)?未见|未提供|无法(确定|判断)|未回报")


def build_judge_prompt(row: dict) -> str:
    rubric = row.get("rubric") or {}
    lines = [f"【问题】\n{row.get('user_input','')}", f"【参考答案】\n{row.get('reference','')}", "【评分 rubric】"]
    for score in range(1, 6):
        lines.append(f"score{score}: {rubric.get(f'score{score}_description','')}")
    lines.append(f"【被测系统回答】\n{row.get('response','')}")
    lines.append("请输出 JSON 评分。")
    return "\n\n".join(lines)


async def judge_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict) -> dict:
    base = {
        "row_number": row["row_number"],
        "original_id": row.get("original_id"),
        "score": None,
        "reason": None,
    }
    if not (row.get("response") or "").strip():
        base["reason"] = "empty response"
        return base
    async with sem:
        payload = {
            "model": JUDGE_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_judge_prompt(row)},
            ],
            "temperature": 0,
            "max_tokens": 4000,
        }
        for attempt in range(3):
            try:
                resp = await client.post("/chat/completions", json=payload, timeout=JUDGE_TIMEOUT)
                resp.raise_for_status()
                message = resp.json()["choices"][0]["message"]
                content = (message.get("content") or message.get("reasoning_content") or "").strip()
                match = re.search(r"\{.*\}", content, re.DOTALL)
                parsed = json.loads(match.group(0))
                score = int(parsed["score"])
                if 1 <= score <= 5:
                    base.update({"score": score, "reason": str(parsed.get("reason", ""))[:200]})
                    return base
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    base["reason"] = f"judge error: {str(exc)[:150]}"
                await asyncio.sleep(2 * (attempt + 1))
        return base


def summarize(scored: list[dict], metas: dict) -> dict:
    def mean(rows: list[dict]) -> float | None:
        values = [r["score"] for r in rows if isinstance(r.get("score"), int)]
        return round(sum(values) / len(values), 2) if values else None

    graded = [r for r in scored if isinstance(r.get("score"), int)]

    def subset(field: str, value) -> list[dict]:
        return [r for r in graded if metas.get(r["row_number"], {}).get(field) == value]

    honest = 0
    uncertain_rows = [r for r in scored if metas.get(r["row_number"], {}).get("uncertainty_sensitive")]
    for row in uncertain_rows:
        if HONEST_PATTERN.search(row.get("response") or ""):
            honest += 1
    return {
        "n_scored": len(graded),
        "mean_score": mean(graded),
        "by_evidence_type": {
            key: mean(subset("evidence_type", key)) for key in sorted({m.get("evidence_type") for m in metas.values() if m.get("evidence_type")})
        },
        "by_disease": {
            key: mean(subset("disease", key)) for key in sorted({m.get("disease") for m in metas.values() if m.get("disease")})
        },
        "uncertainty_sensitive": {
            "n": len(uncertain_rows),
            "mean_score": mean(uncertain_rows),
            "explicit_absence_rate": round(honest / len(uncertain_rows) * 100, 1) if uncertain_rows else None,
        },
        "score_distribution": {
            str(s): sum(1 for r in graded if r["score"] == s) for s in range(1, 6)
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answered", required=True, help="harness 输出 jsonl")
    parser.add_argument("--input", required=True, help="ragas 原始 jsonl(含 reference/rubric)")
    parser.add_argument("--meta", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    base_url = os.environ["EVAL_MINIMAX_BASE_URL"].rstrip("/")
    key = os.environ["EVAL_MINIMAX_KEY"]

    answered = {json.loads(line)["row_number"]: json.loads(line) for line in Path(args.answered).read_text(encoding="utf-8").splitlines() if line.strip()}
    source_rows: dict[int, dict] = {}
    for index, line in enumerate(Path(args.input).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            source_rows[index] = json.loads(line)
    metas = {json.loads(line)["row_number"]: json.loads(line) for line in Path(args.meta).read_text(encoding="utf-8").splitlines() if line.strip()}

    out_path = Path(args.output_prefix + "_scores.jsonl")
    already: dict[int, dict] = {}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row.get("score"), int):
                    already[row["row_number"]] = row

    pending = []
    for number, answer_row in answered.items():
        if number in already:
            continue
        merged = dict(source_rows.get(number, {}))
        merged["response"] = answer_row.get("response") or ""
        merged["row_number"] = number
        merged["original_id"] = answer_row.get("original_id")
        pending.append(merged)

    print(f"待评分 {len(pending)}(已完成 {len(already)})")
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {key}"}) as client:
        results = await asyncio.gather(*(judge_one(client, sem, row) for row in pending))
    scored = list(already.values()) + [r for r in results if r]
    scored.sort(key=lambda r: r["row_number"])
    with out_path.open("w", encoding="utf-8") as handle:
        for row in scored:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = summarize(scored, metas)
    Path(args.output_prefix + "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
