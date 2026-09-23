"""患者问答 RAGAS 测试集与量化汇总。"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from yuxi.services.clinical_ragas_service import extract_tool_contexts, parse_dataset_files, run_summary


def _jsonl(rows):
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode()


def test_parse_existing_three_file_format_binds_patient_by_original_id():
    samples = _jsonl(
        [
            {"user_input": "病例问题", "reference": "病例答案"},
            {"user_input": "指南问题", "reference": "指南答案"},
        ]
    )
    metadata = _jsonl(
        [
            {"row_number": 1, "original_id": "S-1", "retrieval_scope": "session"},
            {"row_number": 2, "original_id": "K-1", "retrieval_scope": "knowledge_base"},
        ]
    )
    mapping = _jsonl([{"question_ids": ["S-1"], "patient_key": "宫颈癌1"}])

    items = parse_dataset_files(samples, metadata, mapping)

    assert [(row["scope"], row["patient_code"]) for row in items] == [
        ("patient", "宫颈癌1"),
        ("knowledge", None),
    ]


def test_parse_rejects_wrong_metadata_alignment_and_unmapped_patient():
    sample = _jsonl([{"user_input": "病例问题", "reference": "病例答案"}])

    with pytest.raises(ValueError, match="行号不一致"):
        parse_dataset_files(sample, _jsonl([{"row_number": 2, "retrieval_scope": "session"}]))
    with pytest.raises(ValueError, match="缺少患者编号映射"):
        parse_dataset_files(sample, _jsonl([{"row_number": 1, "retrieval_scope": "session"}]))
    with pytest.raises(ValueError, match="patient_key"):
        parse_dataset_files(sample, _jsonl([{"row_number": 1, "retrieval_scope": "session"}]), _jsonl([{"question_ids": ["S-1"]}]))


def test_run_summary_excludes_failed_and_missing_metrics_from_denominator():
    run = SimpleNamespace(
        id="run1",
        dataset_id="dataset1",
        status="completed",
        config={},
        results=[
            {"scope": "patient", "status": "completed", "metrics": {"faithfulness": 0.8}},
            {"scope": "patient", "status": "failed", "metrics": {}},
            {"scope": "knowledge", "status": "completed", "metrics": {"faithfulness": 0.6, "context_recall": 1.0}},
        ],
        task_id="task1",
        error=None,
        created_at=datetime(2026, 9, 24),
        finished_at=datetime(2026, 9, 24),
    )

    summary = run_summary(run)

    assert summary["metrics"] == {"faithfulness": pytest.approx(0.7), "context_recall": 1.0}
    assert summary["metric_counts"] == {"faithfulness": 2, "context_recall": 1}
    assert summary["scope_results"]["patient"]["failed"] == 1


def test_tool_contexts_keep_only_model_visible_evidence_chunks():
    """RAGAS 上下文是工具实际返回的文块，而非带元数据的整段 JSON。"""
    output = json.dumps(
        {"kb_id": "kb", "results": [{"id": "c1", "content": "证据一"}, {"id": "c2", "content": "证据二"}]}
    )

    assert extract_tool_contexts("query_kb", output) == [("证据一", "c1"), ("证据二", "c2")]
    assert extract_tool_contexts("search_patient_records", '[{"chunk_id":"p1","content":"病理"}]') == [("病理", "p1")]
    assert extract_tool_contexts("query_kb", "检索失败") == []
