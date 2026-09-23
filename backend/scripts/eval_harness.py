"""ClinEvidence 评测 harness:批量向真实 API 发题,收集回答与来源,断点续跑。

评测隔离约束(红线):请求只携带 user_input;reference/rubric/证据元数据绝不离开本进程。
用法:
  uv run --no-sync python scripts/eval_harness.py --input <ragas.jsonl> \
      --meta <metadata.jsonl> --output <with_response.jsonl> [--limit N] [--concurrency 3]
环境:EVAL_TEMP_PASSWORD(登录凭据)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

BASE_URL = "http://localhost:5050"
AGENT_SLUG = "agent-202609211321"  # 临床助手;评测冻结配置
ALLOWED_REQUEST_KEYS = {"query", "agent_slug", "thread_id", "model_spec", "meta"}
GENERATION_MODEL = "minimax-cn:MiniMax-M2.7-highspeed"
POLL_INTERVAL = 4.0
RUN_TIMEOUT = 420.0


def parse_assistant_content(content: str) -> str:
    """与前端 parseAssistantMessageBody 同口径:剥离 <think> 推理段,只留正文。"""
    text = (content or "").strip()
    open_index = text.find("<think>")
    if open_index == -1:
        return text
    close_index = text.find("</think>")
    if close_index == -1:
        return ""
    return (text[:open_index] + text[close_index + len("</think>") :]).strip()


def extract_sources(messages: list[dict], run_id: str) -> dict:
    """从该 run 的工具调用轨迹提取双通道来源(与前端来源胶囊同一事实)。"""
    patient, knowledge = [], []
    for msg in messages:
        if msg.get("type") != "ai" or msg.get("run_id") != run_id:
            continue
        for call in msg.get("tool_calls") or []:
            name = call.get("name") or (call.get("function") or {}).get("name")
            raw = (call.get("tool_call_result") or {}).get("content")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            items = parsed if isinstance(parsed, list) else (parsed or {}).get("results") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("content"):
                    continue
                if name == "search_patient_records":
                    patient.append(
                        {
                            "chunk_id": item.get("chunk_id"),
                            "document_name": item.get("document_name"),
                            "document_type": item.get("document_type"),
                            "page_number": item.get("page_number"),
                            "score": item.get("score"),
                        }
                    )
                elif name == "query_kb":
                    meta = item.get("metadata") or {}
                    knowledge.append(
                        {
                            "source": meta.get("source") or item.get("source"),
                            "pages": (meta.get("source_metadata") or {}).get("pages"),
                            "score": item.get("score") or meta.get("score"),
                        }
                    )
    return {"patient": patient, "knowledge": knowledge}


class Harness:
    def __init__(self, client: httpx.AsyncClient, token: str, username: str = "yyh"):
        self.username = username
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.patient_by_code: dict[str, str] = {}
        self.disease_patient: dict[str, str] = {}

    async def load_patients(self) -> None:
        resp = await self.client.get("/api/clinical/patients", headers=self.headers)
        resp.raise_for_status()
        rows = resp.json()
        items = rows if isinstance(rows, list) else rows.get("items") or rows.get("patients") or []
        for item in items:
            code = item.get("display_code")
            if item.get("status") == "active" and code:
                self.patient_by_code[code] = item["id"]

    async def create_thread(self, title: str, patient_id: str | None) -> str:
        body: dict = {"agent_id": AGENT_SLUG, "title": title}
        if patient_id:
            body["patient_id"] = patient_id
        resp = await self.client.post("/api/chat/thread", headers=self.headers, json=body)
        resp.raise_for_status()
        return resp.json()["id"]

    async def run_question(self, thread_id: str, question: str, request_id: str) -> dict:
        body = {
            "query": question,
            "agent_slug": AGENT_SLUG,
            "thread_id": thread_id,
            "model_spec": GENERATION_MODEL,
            "meta": {"request_id": request_id},
        }
        # 真·隔离断言:请求体键与值只允许白名单内容,reference/rubric 结构上无法进入
        assert set(body) <= ALLOWED_REQUEST_KEYS, "请求体出现白名单之外的键"
        assert "reference" not in json.dumps(body) and "rubric" not in json.dumps(body)
        resp = await self.client.post("/api/agent/runs", headers=self.headers, json=body)
        if resp.status_code != 200:
            return {"status": "create_failed", "detail": resp.text[:200]}
        run_id = resp.json()["run_id"]
        deadline = time.monotonic() + RUN_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            run = (
                await self.client.get(f"/api/agent/runs/{run_id}", headers=self.headers)
            ).json()["run"]
            if run.get("status") not in ("queued", "dispatched", "running"):
                return {"status": run.get("status"), "run_id": run_id, "run": run}
        return {"status": "timeout", "run_id": run_id, "run": None}

    async def answer_question(self, row: dict, meta: dict, patient_id: str | None) -> dict:
        request_id = "eval-" + uuid.uuid4().hex[:12]
        thread_id = await self.create_thread(f"eval-{meta['original_id']}", patient_id)
        started = time.monotonic()
        outcome = await self.run_question(thread_id, row["user_input"], request_id)
        elapsed = round(time.monotonic() - started, 1)
        response_text, sources, error = "", {"patient": [], "knowledge": []}, None
        status = outcome["status"]
        run_id = outcome.get("run_id")
        if status == "completed":
            history = (
                await self.client.get(f"/api/chat/thread/{thread_id}/history", headers=self.headers)
            ).json()
            messages = history.get("history") or []
            ai_messages = [m for m in messages if m.get("type") == "ai" and m.get("run_id") == run_id]
            if ai_messages:
                response_text = parse_assistant_content(ai_messages[-1].get("content") or "")
            sources = extract_sources(messages, run_id)
        else:
            error = (outcome.get("run") or {}).get("error_message") or status
        return {
            "row_number": row["row_number"],
            "original_id": meta["original_id"],
            "retrieval_scope": meta.get("retrieval_scope"),
            "disease": meta.get("disease"),
            "task_category": meta.get("task_category"),
            "uncertainty_sensitive": meta.get("uncertainty_sensitive"),
            "evidence_file": meta.get("evidence_file"),
            "evidence_pages": meta.get("evidence_pages"),
            "response": response_text,
            "sources": sources,
            "status": status,
            "error": error,
            "elapsed_s": elapsed,
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description="ClinEvidence RAGAS 评测 harness")
    parser.add_argument("--input", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--scope", choices=["all", "patient", "kb"], default="all")
    args = parser.parse_args()

    password = os.environ.get("EVAL_TEMP_PASSWORD")
    if not password:
        raise SystemExit("缺少 EVAL_TEMP_PASSWORD")

    rows = []
    for index, line in enumerate(Path(args.input).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["row_number"] = index
            rows.append(row)
    metas = {json.loads(line)["row_number"]: json.loads(line) for line in Path(args.meta).read_text(encoding="utf-8").splitlines() if line.strip()}
    output_path = Path(args.output)
    done: set[int] = set()
    if output_path.exists():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                # 断点续跑只跳过成功行;失败/超时/中断行重跑时自动重试
                if row.get("status") == "completed":
                    done.add(row["row_number"])
    pending = [r for r in rows if r["row_number"] not in done]
    if args.scope != "all":
        def scope_match(row: dict) -> bool:
            is_patient = metas.get(row["row_number"], {}).get("evidence_type") == "病例"
            return is_patient if args.scope == "patient" else not is_patient

        pending = [r for r in pending if scope_match(r)]
    if args.limit:
        pending = pending[: args.limit]
    print(f"总题数 {len(rows)},已完成 {len(done)},本轮 {len(pending)}")

    # 评测隔离断言:被测请求只允许 user_input;断言材料字段存在即拒绝启动
    # 评测隔离:请求体键白名单在 run_question 内强制;此处校验本地材料完整
    # (评测集必须携带 reference/rubric 供 judge 使用;对抗集可省略)
    has_materials = all(field in rows[0] for field in ("reference", "rubric"))
    print(f"评测隔离:请求体白名单强制;本地材料字段={'完整' if has_materials else '对抗集无材料(允许)'}")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as client:
        login = await client.post(
            "/api/auth/token", data={"username": harness.username, "password": password}
        )
        login.raise_for_status()
        harness = Harness(client, login.json()["access_token"], username=os.environ.get("EVAL_USER", "yyh"))
        await harness.load_patients()
        print(f"可用患者 {len(harness.patient_by_code)}")

        # 病种→患者映射:指南题绑定同病种患者会话(双通道真实场景)
        by_disease: dict[str, str] = {}
        for code in sorted(harness.patient_by_code):
            disease = re.sub(r"\d+$", "", code)
            by_disease.setdefault(disease, harness.patient_by_code[code])
        harness.disease_patient = by_disease

        lock = asyncio.Lock()
        sem = asyncio.Semaphore(args.concurrency)

        async def worker(row: dict) -> None:
            async with sem:
                meta = metas[row["row_number"]]
                patient_id = None
                if meta.get("evidence_type") == "病例":
                    session = meta.get("session_id") or ""
                    base = re.sub(r"(?:病例|病历)\d*$", "", session)
                    patient_id = harness.patient_by_code.get(base)
                    if not patient_id:
                        print(f"[{meta['original_id']}] 找不到患者 {base},跳过", flush=True)
                        return
                else:
                    # KB 题:disease 字段缺失,从证据标题推断病种,绑定同病种患者会话(双通道真实场景)
                    title = (meta.get("evidence_file") or meta.get("source_title") or "")
                    disease = next(
                        (d for d in harness.disease_patient if d in title), None
                    )
                    patient_id = harness.disease_patient.get(disease) if disease else None
                    if not patient_id:
                        # 病种无法识别(如综合指南)时按行号轮转绑定,保持上下文非空
                        codes = sorted(harness.patient_by_code)
                        patient_id = harness.patient_by_code[codes[row["row_number"] % len(codes)]]
                try:
                    result = await harness.answer_question(row, meta, patient_id)
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "row_number": row["row_number"],
                        "original_id": meta["original_id"],
                        "response": "",
                        "status": "harness_error",
                        "error": str(exc)[:300],
                    }
                async with lock:
                    with output_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                print(
                    f"[{result['original_id']}] {result['status']} {result.get('elapsed_s','?')}s "
                    f"len={len(result.get('response') or '')}",
                    flush=True,
                )

        await asyncio.gather(*(worker(row) for row in pending))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
