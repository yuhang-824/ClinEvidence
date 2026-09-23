"""患者问答 RAGAS 数据集的真实 HTTP 与 PostgreSQL 隔离。"""

import json

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ROOT = "/api/clinical/evaluation"


async def test_dataset_upload_requires_auth(test_client):
    """未登录不能提交患者问答数据。"""
    response = await test_client.get(f"{ROOT}/datasets")
    assert response.status_code == 401


async def test_dataset_upload_round_trip_is_owner_scoped(test_client, standard_user, admin_headers):
    """题目和患者映射经 HTTP 入库，只能由上传者读取。"""
    samples = (json.dumps({"user_input": "病例问题", "reference": "参考答案"}, ensure_ascii=False) + "\n").encode()
    metadata = (
        json.dumps({"row_number": 1, "original_id": "S-1", "retrieval_scope": "session"}, ensure_ascii=False) + "\n"
    ).encode()
    mapping = (json.dumps({"question_ids": ["S-1"], "patient_key": "宫颈癌1"}, ensure_ascii=False) + "\n").encode()

    uploaded = await test_client.post(
        f"{ROOT}/datasets",
        headers=standard_user["headers"],
        data={"name": "HTTP 测试集"},
        files={
            "samples": ("samples.jsonl", samples),
            "metadata": ("metadata.jsonl", metadata),
            "scope_map": ("scope.jsonl", mapping),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    dataset_id = uploaded.json()["id"]

    mine = await test_client.get(f"{ROOT}/datasets/{dataset_id}", headers=standard_user["headers"])
    other = await test_client.get(f"{ROOT}/datasets/{dataset_id}", headers=admin_headers)
    assert mine.status_code == 200, mine.text
    assert mine.json()["items"][0]["patient_code"] == "宫颈癌1"
    assert other.status_code == 404
