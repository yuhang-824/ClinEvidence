"""通过真实患者问答 AgentRun 执行 RAGAS 评估。"""

import asyncio
import json
import math
import uuid
from collections import defaultdict
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from yuxi.models.embed import select_embedding_model
from yuxi.models.providers.cache import model_cache
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.clinical_ragas_repository import ClinicalRagasRepository
from yuxi.repositories.agent_run_request_repository import AgentRunRequestRepository
from yuxi.services.agent_request_service import AgentRequestInput, RunOrigin, submit_agent_request
from yuxi.services.agent_run_service import await_agent_run_result
from yuxi.services.conversation_service import create_thread_view
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Message, User
from yuxi.storage.postgres.models_clinical import ClinicalRagasDataset, ClinicalRagasRun
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils import get_docker_safe_url

MAX_DATASET_ITEMS = 500
EVIDENCE_TOOLS = frozenset(
    {
        "search_patient_records",
        "read_evidence_excerpt",
        "query_kb",
        "open_kb_document",
        "find_kb_document",
        "search_file",
    }
)


def extract_tool_contexts(tool_name: str, content: str) -> list[tuple[str, str | None]]:
    """从同 Run 工具审计中提取实际返回的证据文本及身份。"""
    if tool_name not in EVIDENCE_TOOLS:
        return []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        candidates = payload.get("results") if isinstance(payload.get("results"), list) else [payload]
    elif isinstance(payload, list):
        candidates = payload
    else:
        return []
    return [
        (item["content"], str(item.get("chunk_id") or item.get("id") or item.get("file_id") or "") or None)
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"].strip()
    ]


def parse_dataset_files(
    samples: bytes, metadata: bytes | None = None, scope_map: bytes | None = None
) -> list[dict[str, Any]]:
    """读取 RAGAS JSONL，按行号和 original_id 合并病例映射。"""

    def read_jsonl(content: bytes, label: str) -> list[dict[str, Any]]:
        try:
            lines = content.decode("utf-8-sig").splitlines()
            rows = [json.loads(line) for line in lines if line.strip()]
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} 不是有效的 UTF-8 JSONL: {exc}") from exc
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"{label} 每行必须是 JSON 对象")
        return rows

    questions = read_jsonl(samples, "测试题")
    details = read_jsonl(metadata, "题目元数据") if metadata else []
    mappings = read_jsonl(scope_map, "患者映射") if scope_map else []
    if not questions or len(questions) > MAX_DATASET_ITEMS:
        raise ValueError(f"测试集题数必须在 1 到 {MAX_DATASET_ITEMS} 之间")
    if details and len(details) != len(questions):
        raise ValueError("题目元数据与测试题行数不一致")

    code_by_original_id: dict[str, str] = {}
    for mapping in mappings:
        code = mapping.get("patient_key")
        question_ids = mapping.get("question_ids")
        if not isinstance(code, str) or not code.strip() or not isinstance(question_ids, list):
            raise ValueError("患者映射必须包含 patient_key 和 question_ids 数组")
        for question_id in question_ids:
            if not isinstance(question_id, str):
                raise ValueError("患者映射的 question_ids 必须是字符串")
            if question_id in code_by_original_id and code_by_original_id[question_id] != code:
                raise ValueError(f"题号 {question_id} 出现冲突的患者映射")
            code_by_original_id[question_id] = code.strip()

    items = []
    for index, question in enumerate(questions):
        detail = details[index] if details else {}
        if detail.get("row_number") is not None and detail["row_number"] != index + 1:
            raise ValueError(f"第 {index + 1} 题与题目元数据行号不一致")
        user_input = question.get("user_input") or question.get("question")
        reference = question.get("reference")
        scope = detail.get("retrieval_scope") or question.get("retrieval_scope") or question.get("scope")
        scope = {"session": "patient", "knowledge_base": "knowledge"}.get(scope, scope)
        patient_code = (
            question.get("patient_display_code")
            or detail.get("patient_display_code")
            or code_by_original_id.get(detail.get("original_id"))
        )
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError(f"第 {index + 1} 题缺少 user_input")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"第 {index + 1} 题缺少 reference")
        if scope not in {"patient", "knowledge", "mixed"}:
            raise ValueError(f"第 {index + 1} 题的检索范围必须是 patient、knowledge 或 mixed")
        if scope in {"patient", "mixed"} and not patient_code:
            raise ValueError(f"第 {index + 1} 题缺少患者编号映射")
        if patient_code is not None and (not isinstance(patient_code, str) or len(patient_code) > 64):
            raise ValueError(f"第 {index + 1} 题患者编号无效")
        items.append(
            {
                "index": index + 1,
                "user_input": user_input.strip(),
                "reference": reference.strip(),
                "scope": scope,
                "patient_code": patient_code,
                "original_id": detail.get("original_id") or question.get("id"),
                "review_status": detail.get("review_status"),
                "rubric": question.get("rubric"),
            }
        )
    return items


def dataset_summary(dataset: ClinicalRagasDataset) -> dict[str, Any]:
    """返回数据集覆盖及复核状态。"""
    counts: dict[str, int] = defaultdict(int)
    unreviewed = 0
    for item in dataset.items:
        counts[item["scope"]] += 1
        if "待临床" in str(item.get("review_status") or ""):
            unreviewed += 1
    return {
        "id": dataset.id,
        "name": dataset.name,
        "item_count": len(dataset.items),
        "scope_counts": dict(counts),
        "unreviewed_count": unreviewed,
        "created_at": dataset.created_at.isoformat(),
    }


def run_summary(run: ClinicalRagasRun, *, include_results: bool = False) -> dict[str, Any]:
    """只从已持久化的逐题指标计算均值和分组覆盖。"""
    by_scope: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"completed": 0, "failed": 0, "scores": defaultdict(list)}
    )
    scores: dict[str, list[float]] = defaultdict(list)
    for item in run.results or []:
        group = by_scope[item["scope"]]
        group[item["status"]] += 1
        for name, value in item.get("metrics", {}).items():
            if isinstance(value, (int, float)):
                scores[name].append(float(value))
                group["scores"][name].append(float(value))
    payload = {
        "id": run.id,
        "dataset_id": run.dataset_id,
        "status": run.status,
        "config": run.config,
        "completed_items": len(run.results or []),
        "metrics": {name: sum(values) / len(values) for name, values in scores.items()},
        "metric_counts": {name: len(values) for name, values in scores.items()},
        "scope_results": {
            scope: {
                "completed": value["completed"],
                "failed": value["failed"],
                "metrics": {name: sum(values) / len(values) for name, values in value["scores"].items()},
            }
            for scope, value in by_scope.items()
        },
        "task_id": run.task_id,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if include_results:
        payload["results"] = run.results or []
    return payload


async def upload_dataset(
    *, uid: str, name: str, samples: bytes, metadata: bytes | None, scope_map: bytes | None
) -> dict:
    """校验并保存人工测试集。"""
    if not name.strip():
        raise ValueError("测试集名称不能为空")
    items = parse_dataset_files(samples, metadata, scope_map)
    async with pg_manager.get_async_session_context() as db:
        dataset = ClinicalRagasDataset(id=uuid.uuid4().hex, owner_uid=uid, name=name.strip(), items=items)
        db.add(dataset)
        await db.commit()
        await db.refresh(dataset)
        return dataset_summary(dataset)


async def start_run(*, uid: str, dataset_id: str, agent_slug: str, judge_model: str, embedding_model: str) -> dict:
    """校验评估配置，同事务保存 Run 与 Durable Task，提交后发布。"""
    judge = model_cache.get_model_info(judge_model)
    embed = model_cache.get_model_info(embedding_model)
    if (
        judge is None
        or judge.model_type != "chat"
        or judge.provider_type != "openai"
        or embed is None
        or embed.model_type != "embedding"
    ):
        raise ValueError("评判聊天模型或向量模型不可用")
    async with pg_manager.get_async_session_context() as db:
        repo = ClinicalRagasRepository(db)
        dataset = await repo.get_dataset(dataset_id, uid)
        user = await db.scalar(select(User).where(User.uid == uid))
        if dataset is None or user is None:
            raise HTTPException(status_code=404, detail="测试集不存在")
        agent = await AgentRepository(db).get_visible_by_slug(slug=agent_slug, user=user, kind="main")
        if agent is None:
            raise HTTPException(status_code=404, detail="智能体不存在")
        run = ClinicalRagasRun(
            id=uuid.uuid4().hex,
            owner_uid=uid,
            dataset_id=dataset_id,
            status="pending",
            config={"agent_slug": agent_slug, "judge_model": judge_model, "embedding_model": embedding_model},
            results=[],
        )
        db.add(run)
        task = await tasker.create_in_session(
            db,
            name=f"患者问答 RAGAS：{dataset.name}",
            task_type="clinical_ragas_evaluation",
            payload={"run_id": run.id, "uid": uid},
        )
        run.task_id = task.id
        await db.commit()
    await tasker.publish(task)
    return {"id": run.id, "task_id": task.id, "status": "pending"}


async def list_datasets(uid: str) -> list[dict]:
    """列出当前用户的测试集摘要。"""
    async with pg_manager.get_async_session_context() as db:
        rows = await ClinicalRagasRepository(db).list_datasets(uid)
        return [dataset_summary(row) for row in rows]


async def get_options(user: User) -> dict:
    """列出当前用户可执行的智能体及已配置的评判模型。"""
    async with pg_manager.get_async_session_context() as db:
        agents = await AgentRepository(db).list_visible(user=user)
        return {
            "agents": [{"slug": row.slug, "name": row.name} for row in agents],
            "judge_models": [
                {"spec": row.spec, "name": row.display_name}
                for row in model_cache.get_all_specs("chat")
                if row.provider_type == "openai"
            ],
            "embedding_models": [
                {"spec": row.spec, "name": row.display_name} for row in model_cache.get_all_specs("embedding")
            ],
        }


async def get_dataset(uid: str, dataset_id: str) -> dict:
    """读取当前用户的测试题详情。"""
    async with pg_manager.get_async_session_context() as db:
        row = await ClinicalRagasRepository(db).get_dataset(dataset_id, uid)
        if row is None:
            raise HTTPException(status_code=404, detail="测试集不存在")
        return {**dataset_summary(row), "items": row.items}


async def list_runs(uid: str) -> list[dict]:
    """列出当前用户的最近运行。"""
    async with pg_manager.get_async_session_context() as db:
        rows = await ClinicalRagasRepository(db).list_runs(uid)
        return [run_summary(row) for row in rows]


async def get_run(uid: str, run_id: str) -> dict:
    """读取当前用户的逐题结果和已完成评分。"""
    async with pg_manager.get_async_session_context() as db:
        row = await ClinicalRagasRepository(db).get_run(run_id, uid)
        if row is None:
            raise HTTPException(status_code=404, detail="评估运行不存在")
        dataset = await ClinicalRagasRepository(db).get_dataset(row.dataset_id, uid)
        return {**run_summary(row, include_results=True), "total_items": len(dataset.items) if dataset else 0}


async def _answer_one(run: ClinicalRagasRun, item: dict[str, Any]) -> dict[str, Any]:
    """为一题创建独立的患者绑定线程并读取同 Run 的证据。"""
    uid = run.owner_uid
    patient_id = None
    async with pg_manager.get_async_session_context() as db:
        user = await db.scalar(select(User).where(User.uid == uid))
        if user is None:
            raise ValueError("评估用户不存在")
        if item["scope"] in {"patient", "mixed"}:
            patient = await ClinicalRagasRepository(db).get_patient_by_code(item["patient_code"], uid)
            if patient is None:
                raise ValueError(f"患者 {item['patient_code']} 不存在或当前用户不是 Owner")
            if not patient.current_snapshot_id:
                raise ValueError(f"患者 {item['patient_code']} 没有已发布快照")
            patient_id = patient.id

        request_id = f"ragas_thread_{run.id}_{item['index']}"
        thread = await create_thread_view(
            agent_slug=run.config["agent_slug"],
            request_id=request_id,
            title=f"RAGAS #{item['index']}",
            metadata={"source": "clinical_ragas", "evaluation_run_id": run.id},
            patient_id=patient_id,
            db=db,
            current_uid=uid,
        )
        submitted = await submit_agent_request(
            request_input=AgentRequestInput(
                agent_slug=run.config["agent_slug"],
                thread_id=thread["id"],
                request_id=f"ragas_request_{run.id}_{item['index']}",
                input_message=build_chat_input_message(item["user_input"]),
                origin=RunOrigin(source="clinical_ragas", channel="worker", metadata={"evaluation_run_id": run.id}),
                queue_policy="enqueue",
                tool_approval_mode="default",
            ),
            current_user=user,
            db=db,
        )

    agent_run_id = submitted.get("run_id")
    for _ in range(60):
        if agent_run_id:
            break
        await asyncio.sleep(1)
        async with pg_manager.get_async_session_context() as db:
            request = await AgentRunRequestRepository(db).get_by_request_id(
                f"ragas_request_{run.id}_{item['index']}"
            )
            if request is None or request.status in {"failed", "cancelled", "rejected"}:
                raise ValueError("Agent 请求排队失败或已取消")
            agent_run_id = request.dispatched_run_id
    if not agent_run_id:
        raise ValueError("Agent 请求排队超过 60 秒，未获得 Run ID")
    answer = await await_agent_run_result(run_id=agent_run_id, current_uid=uid)
    if answer["status"] != "completed":
        return {
            "agent_run_id": agent_run_id,
            "error": f"AgentRun {answer['status']}: {answer.get('error') or '没有回答'}",
        }
    response = str(answer.get("output") or "").strip()
    if not response:
        return {"agent_run_id": agent_run_id, "error": "AgentRun 没有最终回答"}

    async with pg_manager.get_async_session_context() as db:
        audits = await db.scalars(
            select(Message)
            .where(
                Message.run_id == agent_run_id,
                Message.message_type == "tool_audit",
                Message.execution_status == "completed",
            )
            .order_by(Message.sequence.asc().nullslast(), Message.id.asc())
        )
        contexts = []
        sources = []
        context_ids = []
        for audit in audits:
            meta = audit.extra_metadata if isinstance(audit.extra_metadata, dict) else {}
            name = meta.get("tool_name")
            for content, context_id in extract_tool_contexts(name, audit.content):
                contexts.append(content)
                context_ids.append(context_id)
                sources.append(
                    "patient" if name in {"search_patient_records", "read_evidence_excerpt"} else "knowledge"
                )
    return {
        "agent_run_id": agent_run_id,
        "response": response,
        "retrieved_contexts": contexts,
        "context_sources": sources,
        "context_ids": context_ids,
        "missing_sources": sorted(
            ({"patient", "knowledge"} if item["scope"] == "mixed" else {item["scope"]}) - set(sources)
        ),
        **({"error": "该 AgentRun 没有可用于 RAGAS 的病例或知识库工具证据"} if not contexts else {}),
    }


async def _score_one(item: dict[str, Any], answer: dict[str, Any], config: dict[str, str]) -> tuple[dict, dict]:
    """调用 RAGAS 四项指标并逐项保留评判失败原因。"""
    from openai import AsyncOpenAI
    from ragas.embeddings.base import BaseRagasEmbedding
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

    class ProjectEmbeddings(BaseRagasEmbedding):
        """将项目已配置的向量模型接入 RAGAS。"""

        def __init__(self, spec: str):
            super().__init__()
            self.model = select_embedding_model(spec)

        def embed_text(self, text: str, **kwargs) -> list[float]:
            return self.model.encode_queries([text])[0]

        async def aembed_text(self, text: str, **kwargs) -> list[float]:
            return (await self.model.aencode_queries([text]))[0]

    judge = model_cache.get_model_info(config["judge_model"])
    if judge is None:
        raise ValueError("评判模型配置已失效")
    client = AsyncOpenAI(api_key=judge.api_key, base_url=get_docker_safe_url(judge.base_url))
    llm = llm_factory(judge.model_id, client=client)
    embeddings = ProjectEmbeddings(config["embedding_model"])
    common = {"user_input": item["user_input"], "response": answer["response"]}
    with_context = {"user_input": item["user_input"], "retrieved_contexts": answer["retrieved_contexts"]}
    checks = {
        "context_precision": (ContextPrecision(llm=llm), {**with_context, "reference": item["reference"]}),
        "context_recall": (ContextRecall(llm=llm), {**with_context, "reference": item["reference"]}),
        "faithfulness": (Faithfulness(llm=llm), {**with_context, "response": answer["response"]}),
        "answer_relevancy": (AnswerRelevancy(llm=llm, embeddings=embeddings), common),
    }
    scores, errors = {}, {}
    try:
        for name, (metric, arguments) in checks.items():
            try:
                result = await metric.ascore(**arguments)
                value = float(result.value)
                if not math.isfinite(value):
                    raise ValueError("RAGAS 返回非有限分数")
                scores[name] = value
            except Exception as exc:
                errors[name] = str(exc)
    finally:
        await client.close()
    return scores, errors


async def run_clinical_ragas_task(context: TaskContext) -> dict[str, Any]:
    """由 Durable Task 逐题保存真实回答与 RAGAS 评分。"""
    run_id = context.payload["run_id"]
    uid = context.payload["uid"]
    async with pg_manager.get_async_session_context() as db:
        repo = ClinicalRagasRepository(db)
        run = await repo.get_run(run_id, uid)
        dataset = await repo.get_dataset(run.dataset_id, uid) if run else None
        if run is None or dataset is None:
            raise ValueError("评估运行或测试集不存在")
        items = dataset.items
        config = dict(run.config)
        processed_indices = {result["index"] for result in run.results or []}

    async def mark_running(session, _task_record):
        record = await session.get(ClinicalRagasRun, run_id)
        record.status = "running"

    await context.run_owned_transaction(mark_running)
    for index, item in enumerate(items):
        if item["index"] in processed_indices:
            continue
        await context.raise_if_cancelled()
        await context.set_progress(100 * index / len(items), f"正在评估 {index + 1}/{len(items)}")
        result = {"index": item["index"], "scope": item["scope"], "original_id": item.get("original_id")}
        try:
            answer = await _answer_one(run, item)
            result.update(answer)
            if answer.get("error"):
                raise ValueError(answer["error"])
            scores, errors = await _score_one(item, answer, config)
            result["metrics"] = scores
            result["metric_errors"] = errors
            result["status"] = "completed" if scores else "failed"
            if not scores:
                result["error"] = "全部 RAGAS 指标计算失败"
        except Exception as exc:
            result.update(status="failed", error=str(exc), metrics={})

        async def persist_item(session, _task_record):
            record = await session.get(ClinicalRagasRun, run_id, with_for_update=True)
            record.results = [*(record.results or []), result]

        await context.run_owned_transaction(persist_item)

    await context.set_progress(100, "评分完成")
    return {"run_id": run_id, "item_count": len(items)}


async def finish_clinical_ragas_task(session, task_record, result) -> None:
    """在 Task 成功事务内设置评估终态。"""
    run = await session.get(ClinicalRagasRun, task_record.payload["run_id"])
    if run is not None:
        run.status = "completed"
        run.finished_at = utc_now_naive()


async def fail_clinical_ragas_task(session, task_record, error: str) -> None:
    """在 Task 失败事务内保存可观察原因。"""
    run = await session.get(ClinicalRagasRun, task_record.payload["run_id"])
    if run is not None:
        run.status = "failed"
        run.error = error
        run.finished_at = utc_now_naive()
