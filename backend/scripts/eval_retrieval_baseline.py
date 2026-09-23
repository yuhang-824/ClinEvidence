"""ClinEvidence 检索基线评测:对 100/800 题直评双通道检索,不经 LLM。

口径:
- 患者通道:题目(剥离"针对X患者"前缀)embed 后在患者当前已发布快照内检索,
  文块→文档版本→(按患者内上传顺序)映射回 manifest 文件名,与证据文件对比;
  页级命中要求文块页码与 evidence_pages 有交集。
- 知识通道:knowledge manager retrieve(final_top_k=10),metadata.source 与
  证据文件 basename 对比;页级同上。
输出:逐题 jsonl + 汇总 json(Recall@5/10、页级命中@10、MRR,按通道与病种分层)。
在 api 容器内运行:python scripts/eval_retrieval_baseline.py --questions ... --meta ... --scope-map ... --manifest ... --out-prefix /tmp/eval/retrieval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import defaultdict
from pathlib import Path

PATIENT_EMBEDDING_SPEC_ENV = "YUXI_PATIENT_EMBEDDING_SPEC"


def strip_patient_prefix(question: str) -> str:
    import re

    return re.sub(r"^针对[^,，]*患者的病历《[^》]*》，", "", question).strip() or question.strip()


def basename(path: str | None) -> str:
    return os.path.basename(path or "")


async def eval_patient_channel(question: str, patient_key: str, evidence_file: str, evidence_pages: list[int], top_k: int) -> dict:
    from sqlalchemy import select

    from yuxi.knowledge.patient_index import search_patient_chunks
    from yuxi.services.clinical_retrieval_service import _embed_query, snapshot_member_tuples
    from yuxi.storage.postgres.models_clinical import (
        Patient,
        PatientChunk,
        PatientDocument,
        PatientDocumentVersion,
        PatientSnapshot,
    )
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        patient = (
            await db.execute(select(Patient).where(Patient.display_code == patient_key, Patient.status == "active"))
        ).scalars().first()
        if patient is None or not patient.current_snapshot_id:
            return {"error": f"患者缺失或无快照: {patient_key}"}
        snapshot = await db.get(PatientSnapshot, patient.current_snapshot_id)
        if snapshot is None or snapshot.status != "published":
            return {"error": f"快照未发布: {patient_key}"}
        members = await snapshot_member_tuples(db, snapshot)
        # 文档按创建顺序映射回 manifest 上传顺序
        doc_rows = (
            await db.execute(
                select(PatientDocument.id, PatientDocumentVersion.version)
                .join(PatientDocumentVersion, PatientDocumentVersion.document_id == PatientDocument.id)
                .where(PatientDocument.patient_id == patient.id)
                .order_by(PatientDocument.created_at, PatientDocumentVersion.version)
            )
        ).all()
        seen: set[str] = set()
        doc_order: list[str] = []
        for doc_id, _version in doc_rows:
            if doc_id not in seen:
                seen.add(doc_id)
                doc_order.append(doc_id)
        embedding = await _embed_query(question)
        hits = await search_patient_chunks(
            patient_id=patient.id, snapshot_members=members, query_embedding=embedding, top_k=top_k
        )
        hydrated = []
        for hit in hits:
            chunk = await db.get(PatientChunk, hit["chunk_id"])
            if chunk is None:
                continue
            hydrated.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "page_number": chunk.page_number,
                    "score": hit.get("score"),
                }
            )
    return {
        "docs": [item["document_id"] for item in hydrated],
        "ranks": [
            {"document_id": item["document_id"], "page": item["page_number"], "score": item["score"]}
            for item in hydrated
        ],
        "doc_order": doc_order,
    }


async def eval_kb_channel(question: str, evidence_file: str, evidence_pages: list[int], top_k: int) -> dict:
    from yuxi.knowledge.runtime import knowledge_base

    kb = (await knowledge_base.get_databases_by_uid(os.environ.get("EVAL_OPERATOR_UID", "yyh")))
    target = next((db for db in kb if db.name == "指南"), None)
    if target is None:
        return {"error": "指南知识库不存在"}
    output = await knowledge_base.retrieve(target.kb_id, question, final_top_k=top_k)
    results = output.get("results") if isinstance(output, dict) else []
    ranks = []
    for item in results or []:
        meta = item.get("metadata") or {}
        ranks.append(
            {
                "source": basename(meta.get("source") or item.get("source")),
                "pages": (meta.get("source_metadata") or {}).get("pages") or [],
                "score": item.get("score") or meta.get("score"),
            }
        )
    return {"ranks": ranks}


def grade(ranks: list[dict], evidence_file: str, evidence_pages: list[int]) -> dict:
    """文档级命中=检索文档名等于证据文件名;页级命中再要求页码与证据页有交集。"""
    ev_name = basename(evidence_file)
    doc_hit_rank = None
    page_hit_rank = None
    for index, item in enumerate(ranks, 1):
        matched = item.get("_doc_name") == ev_name if "_doc_name" in item else item.get("source") == ev_name
        if matched and doc_hit_rank is None:
            doc_hit_rank = index
            pages = item.get("pages") or ([item.get("page")] if item.get("page") else [])
            if not evidence_pages or (set(pages or []) & set(evidence_pages)):
                page_hit_rank = index
                break
    return {"doc_hit_rank": doc_hit_rank, "page_hit_rank": page_hit_rank}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = []
    for index, line in enumerate(Path(args.questions).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["row_number"] = index
            rows.append(row)
    metas = {json.loads(line)["row_number"]: json.loads(line) for line in Path(args.meta).read_text(encoding="utf-8").splitlines() if line.strip()}
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    files_by_patient: dict[str, list[str]] = defaultdict(list)
    for item in manifest.get("files", []):
        files_by_patient[item["patient_key"]].append(basename(item.get("original_filename")))

    detail_path = Path(args.out_prefix + "_detail.jsonl")
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    pending = rows if not args.limit else rows[: args.limit]
    for row in pending:
        meta = metas[row["row_number"]]
        question = strip_patient_prefix(row["user_input"])
        evidence_file = meta.get("evidence_file") or ""
        evidence_pages = meta.get("evidence_pages") or []
        record = {"row_number": row["row_number"], "original_id": meta["original_id"], "scope": meta.get("retrieval_scope")}
        try:
            if meta.get("evidence_type") == "病例":
                patient_key = re.sub(r"(?:病例|病历)\d*$", "", meta.get("session_id") or "")
                manifest_names = files_by_patient.get(patient_key, [])
                outcome = await eval_patient_channel(question, patient_key, evidence_file, evidence_pages, args.top_k)
                if "error" in outcome:
                    record.update({"error": outcome["error"]})
                    results.append(record)
                    continue
                doc_order = outcome["doc_order"]
                name_by_doc = dict(zip(doc_order, manifest_names, strict=False))
                ranks = [
                    {"document_id": r["document_id"], "_doc_name": name_by_doc.get(r["document_id"]), "page": r["page"]}
                    for r in outcome["ranks"]
                ]
                grade_out = grade(ranks, evidence_file, evidence_pages)
            else:
                outcome = await eval_kb_channel(question, evidence_file, evidence_pages, args.top_k)
                if "error" in outcome:
                    record.update({"error": outcome["error"]})
                    results.append(record)
                    continue
                grade_out = grade(outcome["ranks"], evidence_file, evidence_pages)
            record.update(grade_out)
        except Exception as exc:  # noqa: BLE001
            record.update({"error": str(exc)[:300]})
        results.append(record)
        print(f"[{meta['original_id']}] doc_rank={record.get('doc_hit_rank')} page_rank={record.get('page_hit_rank')} err={record.get('error','')}", flush=True)

    with detail_path.open("w", encoding="utf-8") as handle:
        for record in results:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summarize(subset: list[dict]) -> dict:
        graded = [r for r in subset if "doc_hit_rank" in r]
        if not graded:
            return {"n": len(subset), "note": "no graded rows"}
        def recall_at(k: int) -> float:
            return round(sum(1 for r in graded if r["doc_hit_rank"] and r["doc_hit_rank"] <= k) / len(graded) * 100, 1)
        def page_at(k: int) -> float:
            return round(sum(1 for r in graded if r["page_hit_rank"] and r["page_hit_rank"] <= k) / len(graded) * 100, 1)
        mrr = round(sum(1 / r["doc_hit_rank"] for r in graded if r["doc_hit_rank"]) / len(graded), 3)
        return {"n": len(graded), "recall@5": recall_at(5), "recall@10": recall_at(10), "page_hit@10": page_at(10), "mrr": mrr}

    summary = {
        "all": summarize(results),
        "patient_channel": summarize([r for r in results if r.get("scope") == "session"]),
        "kb_channel": summarize([r for r in results if r.get("scope") != "session"]),
        "by_disease": {
            disease: summarize([r for r in results if metas[r["row_number"]].get("disease") == disease])
            for disease in sorted({str(metas[r["row_number"]].get("disease")) for r in results if metas.get(r["row_number"])})
        },
    }
    Path(args.out_prefix + "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
