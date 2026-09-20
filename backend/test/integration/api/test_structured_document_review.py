"""真实 HTTP、解析 worker 与索引验证结构审核边界。"""

import asyncio
import os
import uuid

import pymupdf
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def wait_task(client, headers, task_id):
    """等待持久化终态，不把排队成功当作业务成功。"""
    for _ in range(240):
        response = await client.get(f"/api/tasks/{task_id}", headers=headers)
        response.raise_for_status()
        task = response.json()["task"]
        if task["status"] in {"success", "failed", "cancelled"}:
            return task
        await asyncio.sleep(1)
    pytest.fail("文档任务未在四分钟内结束")


async def test_pdf_structure_review_to_index(test_client, admin_headers):
    """合成文档逐页审核后才可入库，源版本与实际片段必须一致。"""
    created = await test_client.post(
        "/api/knowledge/databases",
        headers=admin_headers,
        json={
            "database_name": f"pytest_structured_{uuid.uuid4().hex[:8]}",
            "description": "Synthetic test only",
            "embedding_model_spec": os.getenv("TEST_EMBEDDING_MODEL", "siliconflow-cn:Pro/BAAI/bge-m3"),
            "kb_type": "milvus",
        },
    )
    created.raise_for_status()
    kb = created.json()["kb_id"]
    base = f"/api/knowledge/databases/{kb}/documents"
    try:
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "TEST ONLY: Original evidence review", fontsize=16)
            page.insert_text((50, 90), "First: inspect source. Second: verify complete text.", fontsize=12)
            raw = pdf.tobytes()
        upload = await test_client.post(
            "/api/knowledge/files/upload",
            params={"kb_id": kb},
            files={"file": ("fixture.pdf", raw, "application/pdf")},
            headers=admin_headers,
        )
        upload.raise_for_status()
        item = upload.json()
        added = await test_client.post(
            base + "/add",
            headers=admin_headers,
            json={
                "items": [item["file_path"]],
                "params": {
                    "content_hashes": {item["file_path"]: item["content_hash"]},
                    "file_sizes": {item["file_path"]: item["size"]},
                },
            },
        )
        added.raise_for_status()
        file_id = added.json()["items"][0]["file_id"]
        review_url = base + f"/{file_id}/review"
        parsing = await test_client.post(base + "/parse", headers=admin_headers, json={"file_ids": [file_id]})
        parsing.raise_for_status()
        await wait_task(test_client, admin_headers, parsing.json()["task_id"])
        record = await test_client.get(review_url, headers=admin_headers)
        record.raise_for_status()
        revision = record.json()["revisions"][-1]
        assert revision["report"]["structure"]["parser"] == "docling"
        assert "verify complete text" in revision["content"]
        assert revision["approved_at"] is None
        rejected = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 1})
        assert rejected.status_code == 400 and "尚未完成" in rejected.text
        source = await test_client.get(review_url + "/source", headers=admin_headers, params={"version": 1, "page": 1})
        assert source.status_code == 200 and source.content.startswith(b"\x89PNG")
        missing_page = await test_client.get(
            review_url + "/source", headers=admin_headers, params={"version": 1, "page": 2}
        )
        assert missing_page.status_code == 404
        unauthenticated = await test_client.get(review_url + "/source", params={"version": 1, "page": 1})
        assert unauthenticated.status_code in {401, 403}
        original = await test_client.get(review_url + "/source", headers=admin_headers, params={"version": 1})
        assert original.json()["schema_name"] == "DoclingDocument"
        structure = revision["report"]["structure"]
        change = {
            "blocks": [
                {k: b[k] for k in ("id", "page", "text", "kind", "level", "excluded", "note")}
                for b in structure["blocks"]
            ],
            "pages": [
                {
                    "page": p["page"],
                    "checks": {key: True for key in p["checks"]},
                    "note": "Checked against synthetic source text",
                }
                for p in structure["pages"]
            ],
        }
        change["blocks"][0]["bbox"] = [0, 0, 1, 1]
        tampered = await test_client.post(
            review_url, headers=admin_headers, json={"action": "save", "version": 1, "structure": change}
        )
        assert tampered.status_code == 400
        change["blocks"][0].pop("bbox")
        saved = await test_client.post(
            review_url, headers=admin_headers, json={"action": "save", "version": 1, "structure": change}
        )
        saved.raise_for_status()
        # 未审核的草稿原地更新：保存不产生新版本
        assert saved.json()["revisions"][-1]["version"] == 1
        assert saved.json()["revisions"][-1]["approved_at"] is None
        approved = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 1})
        approved.raise_for_status()
        # 已审核后再修订才生成新草稿版本，且新版本未审核
        revised = await test_client.post(
            review_url, headers=admin_headers, json={"action": "save", "version": 1, "structure": change}
        )
        revised.raise_for_status()
        assert revised.json()["revisions"][-1]["version"] == 2
        assert revised.json()["revisions"][-1]["approved_at"] is None
        reapproved = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 2})
        reapproved.raise_for_status()
        preview = await test_client.post(
            base + f"/{file_id}/chunk-preview", headers=admin_headers, json={"version": 2, "chunk_token_num": 512}
        )
        preview.raise_for_status()
        indexing = await test_client.post(
            base + "/index", headers=admin_headers, json={"file_ids": [file_id], "params": preview.json()["params"]}
        )
        indexing.raise_for_status()
        await wait_task(test_client, admin_headers, indexing.json()["task_id"])
        content = await test_client.get(base + f"/{file_id}/content", headers=admin_headers)
        content.raise_for_status()
        chunks = content.json()["lines"]
        assert chunks and len(chunks) == len(preview.json()["chunks"])
        assert all(c["source_metadata"]["revision"] == 2 and c["source_metadata"]["source_blocks"] for c in chunks)
        record = await test_client.get(review_url, headers=admin_headers)
        assert record.json()["revisions"][-1]["indexed_at"]
    finally:
        deleted = await test_client.delete(f"/api/knowledge/databases/{kb}", headers=admin_headers)
        assert deleted.status_code in {200, 404}


async def test_machine_verified_pages_need_no_human_signature(test_client, admin_headers):
    """解析器证据充分的页免人工签核即可审核入库；被人工改动的页重新要求核验。"""
    created = await test_client.post(
        "/api/knowledge/databases",
        headers=admin_headers,
        json={
            "database_name": f"pytest_auto_review_{uuid.uuid4().hex[:8]}",
            "description": "Synthetic test only",
            "embedding_model_spec": os.getenv("TEST_EMBEDDING_MODEL", "siliconflow-cn:Pro/BAAI/bge-m3"),
            "kb_type": "milvus",
        },
    )
    created.raise_for_status()
    kb = created.json()["kb_id"]
    base = f"/api/knowledge/databases/{kb}/documents"
    try:
        with pymupdf.open() as pdf:
            for number in (1, 2):
                page = pdf.new_page()
                page.insert_text((50, 50), f"TEST ONLY page {number}", fontsize=16)
                page.insert_text((50, 90), "Machine verified body text for review contract.", fontsize=12)
            raw = pdf.tobytes()
        upload = await test_client.post(
            "/api/knowledge/files/upload",
            params={"kb_id": kb},
            files={"file": ("auto_review.pdf", raw, "application/pdf")},
            headers=admin_headers,
        )
        upload.raise_for_status()
        item = upload.json()
        added = await test_client.post(
            base + "/add",
            headers=admin_headers,
            json={
                "items": [item["file_path"]],
                "params": {
                    "content_hashes": {item["file_path"]: item["content_hash"]},
                    "file_sizes": {item["file_path"]: item["size"]},
                },
            },
        )
        added.raise_for_status()
        file_id = added.json()["items"][0]["file_id"]
        review_url = base + f"/{file_id}/review"
        parsing = await test_client.post(base + "/parse", headers=admin_headers, json={"file_ids": [file_id]})
        parsing.raise_for_status()
        await wait_task(test_client, admin_headers, parsing.json()["task_id"])
        record = await test_client.get(review_url, headers=admin_headers)
        record.raise_for_status()
        structure = record.json()["revisions"][-1]["report"]["structure"]
        rules = [p.get("auto_review", {}).get("rule") for p in structure["pages"]]
        assert rules == ["no-anomaly/v1", "no-anomaly/v1"]
        # 机器核验页不需要任何人工签核，直接审批通过
        approved = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 1})
        approved.raise_for_status()
        assert approved.json()["revisions"][-1]["approved_at"]

        def payload(blocks, pages):
            return {"action": "save", "version": 1, "structure": {"blocks": blocks, "pages": pages}}

        blocks = [
            {k: b[k] for k in ("id", "page", "text", "kind", "level", "excluded", "note")} for b in structure["blocks"]
        ]
        unsigned = [
            {"page": p["page"], "checks": dict.fromkeys(p["checks"], False), "note": ""} for p in structure["pages"]
        ]
        # 人工改动第 2 页正文：该页机器核验失效，第 1 页保持机器核验
        edited = [dict(b) for b in blocks]
        target = next(b for b in edited if b["page"] == 2 and b["kind"] == "paragraph")
        target["text"] = target["text"] + " 人工修订"
        saved = await test_client.post(review_url, headers=admin_headers, json=payload(edited, unsigned))
        saved.raise_for_status()
        latest = saved.json()["revisions"][-1]
        assert latest["version"] == 2
        assert "auto_review" not in latest["report"]["structure"]["pages"][1]
        assert latest["report"]["structure"]["pages"][0]["auto_review"]["rule"] == "no-anomaly/v1"
        blocked = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 2})
        assert blocked.status_code == 400 and "第 2 页尚未完成" in blocked.text

        signed = [
            {
                "page": p["page"],
                "checks": {key: p["page"] == 2 for key in p["checks"]},
                "note": "Checked against synthetic source text" if p["page"] == 2 else "",
            }
            for p in structure["pages"]
        ]
        saved = await test_client.post(review_url, headers=admin_headers, json=payload(edited, signed))
        saved.raise_for_status()
        approved = await test_client.post(review_url, headers=admin_headers, json={"action": "approve", "version": 2})
        approved.raise_for_status()
        preview = await test_client.post(
            base + f"/{file_id}/chunk-preview", headers=admin_headers, json={"version": 2, "chunk_token_num": 512}
        )
        preview.raise_for_status()
        indexing = await test_client.post(
            base + "/index", headers=admin_headers, json={"file_ids": [file_id], "params": preview.json()["params"]}
        )
        indexing.raise_for_status()
        await wait_task(test_client, admin_headers, indexing.json()["task_id"])
        content = await test_client.get(base + f"/{file_id}/content", headers=admin_headers)
        content.raise_for_status()
        chunks = content.json()["lines"]
        assert chunks and all(c["source_metadata"]["revision"] == 2 for c in chunks)
        assert any("人工修订" in c["content"] for c in chunks)
    finally:
        deleted = await test_client.delete(f"/api/knowledge/databases/{kb}", headers=admin_headers)
        assert deleted.status_code in {200, 404}
