"""Model AIMessage 生命周期审计 Repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.storage.postgres.models_business import MODEL_AUDIT_MESSAGE_TYPE, Message


class ModelMessageAuditRepository:
    """以当前 Run lease 为边界持久化 Model 生命周期。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        self.run_repo = AgentRunRepository(db_session)

    async def start(
        self,
        *,
        run_id: str,
        request_id: str,
        thread_id: str,
        worker_id: str,
        operation_id: str,
        sequence: int,
        started_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Message, bool]:
        """幂等创建 running AIMessage，并返回是否首次创建。"""
        normalized_operation_id = operation_id.strip()
        if not normalized_operation_id:
            raise ValueError("Model operation_id 不能为空")
        if sequence < 0:
            raise ValueError("Model sequence 不能为负数")

        run = await self.run_repo.lock_output_persistence(
            run_id,
            worker_id=worker_id,
            conversation_thread_id=thread_id,
            request_id=request_id,
        )
        if run is None:
            raise ValueError(f"AgentRun 不存在: {run_id}")

        existing = await self._get(run_id, normalized_operation_id)
        if existing is None and (metadata or {}).get("model_run_id"):
            existing = await self._get(run_id, f"input:{metadata['model_run_id']}")
            if existing is not None:
                self._require_same_owner(existing, conversation_id=run.conversation_id, request_id=request_id)
                existing.operation_id = normalized_operation_id
                existing.sequence = sequence
                existing.extra_metadata = {**dict(existing.extra_metadata or {}), **dict(metadata or {})}
                await self.db.flush()
                return existing, True
        if existing is not None:
            self._require_same_owner(existing, conversation_id=run.conversation_id, request_id=request_id)
            self._require_same_start(existing, sequence=sequence)
            return existing, False

        message = Message(
            conversation_id=run.conversation_id,
            role="assistant",
            content="",
            message_type=MODEL_AUDIT_MESSAGE_TYPE,
            extra_metadata=dict(metadata or {}),
            run_id=run.id,
            request_id=request_id,
            delivery_status="complete",
            operation_id=normalized_operation_id,
            started_at=started_at,
            sequence=sequence,
            execution_status="running",
        )
        self.db.add(message)
        await self.db.flush()
        await self.db.refresh(message)
        return message, True

    async def record_input(
        self,
        *,
        run_id: str,
        request_id: str,
        thread_id: str,
        worker_id: str,
        model_run_id: str,
        body: dict,
        captured_at: datetime,
    ) -> Message:
        """持久化发送前输入，并按模型调用 ID 合并早到的生命周期事件。"""
        run = await self.run_repo.lock_output_persistence(
            run_id, worker_id=worker_id, conversation_thread_id=thread_id, request_id=request_id
        )
        if run is None:
            raise ValueError(f"AgentRun 不存在: {run_id}")
        result = await self.db.execute(
            select(Message).where(
                Message.run_id == run_id,
                Message.role == "assistant",
                Message.extra_metadata["model_run_id"].as_string() == model_run_id,
            )
        )
        message = result.scalar_one_or_none()
        if message is None:
            message = Message(
                conversation_id=run.conversation_id,
                role="assistant",
                content="",
                message_type=MODEL_AUDIT_MESSAGE_TYPE,
                run_id=run_id,
                request_id=request_id,
                delivery_status="complete",
                operation_id=f"input:{model_run_id}",
                started_at=captured_at,
                execution_status="running",
                extra_metadata={"audit_kind": "model", "model_run_id": model_run_id},
            )
            self.db.add(message)
        self._require_same_owner(message, conversation_id=run.conversation_id, request_id=request_id)
        metadata = dict(message.extra_metadata or {})
        previous = metadata.get("model_input")
        if previous and previous["body"] != body:
            raise ValueError("同一模型调用的重试不能覆盖不同输入")
        metadata["model_input"] = {
            "body": body,
            "captured_at": captured_at.isoformat() + "Z",
            "source": "http_request_body",
            "http_attempts": (previous or {}).get("http_attempts", 0) + 1,
        }
        message.extra_metadata = metadata
        await self.db.flush()
        return message

    async def finish(
        self,
        *,
        run_id: str,
        request_id: str,
        thread_id: str,
        worker_id: str,
        operation_id: str,
        content: str,
        finished_at: datetime,
        duration_ms: int | None,
        usage: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """完成同一 AIMessage；重复 finish 只接受相同业务结果。"""
        normalized_operation_id = operation_id.strip()
        if not normalized_operation_id:
            raise ValueError("Model operation_id 不能为空")
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("Model duration_ms 不能为负数")

        run = await self.run_repo.lock_output_persistence(
            run_id,
            worker_id=worker_id,
            conversation_thread_id=thread_id,
            request_id=request_id,
        )
        if run is None:
            raise ValueError(f"AgentRun 不存在: {run_id}")

        message = await self._get(run_id, normalized_operation_id)
        if message is None:
            raise ValueError("Model finish 缺少对应的 start 事实")
        self._require_same_owner(message, conversation_id=run.conversation_id, request_id=request_id)

        normalized_usage = dict(usage) if isinstance(usage, dict) else None
        if message.execution_status == "completed":
            if message.content != content or message.usage != normalized_usage:
                raise ValueError("已完成 Model 审计事实不能被不同结果覆盖")
            return message
        if message.execution_status != "running":
            raise ValueError(f"Model 审计事实不能从 {message.execution_status} 转为 completed")

        message.content = content
        message.finished_at = finished_at
        message.duration_ms = duration_ms
        message.execution_status = "completed"
        message.usage = normalized_usage
        message.extra_metadata = {**dict(message.extra_metadata or {}), **dict(metadata or {})}
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def get(self, *, run_id: str, operation_id: str) -> Message | None:
        """按同一 Run 的稳定来源键读取审计消息。"""
        return await self._get(run_id, operation_id)

    async def fail_input(
        self,
        *,
        run_id: str,
        request_id: str,
        thread_id: str,
        worker_id: str,
        model_run_id: str,
        error_type: str,
        finished_at: datetime,
    ) -> None:
        """模型报错时关闭已采集的输入，后续重试使用新的模型调用 ID。"""
        await self.run_repo.lock_output_persistence(
            run_id, worker_id=worker_id, conversation_thread_id=thread_id, request_id=request_id
        )
        result = await self.db.execute(
            select(Message).where(
                Message.run_id == run_id,
                Message.role == "assistant",
                Message.extra_metadata["model_run_id"].as_string() == model_run_id,
                Message.execution_status == "running",
            )
        )
        message = result.scalar_one_or_none()
        if message is not None:
            message.execution_status = "failed"
            message.finished_at = finished_at
            message.extra_metadata = {**dict(message.extra_metadata or {}), "model_error_type": error_type}
            await self.db.flush()

    async def list_for_run(self, run_id: str) -> list[Message]:
        """返回 Run 的 Model 时间线，包括已发布为普通历史的最终消息。"""
        result = await self.db.execute(
            select(Message)
            .where(
                Message.run_id == run_id,
                Message.operation_id.is_not(None),
                Message.role == "assistant",
            )
            .order_by(Message.sequence.asc(), Message.id.asc())
        )
        return list(result.scalars().all())

    async def _get(self, run_id: str, operation_id: str) -> Message | None:
        result = await self.db.execute(
            select(Message).where(
                Message.run_id == run_id,
                Message.operation_id == operation_id,
                Message.role == "assistant",
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _require_same_owner(message: Message, *, conversation_id: int, request_id: str) -> None:
        if (
            message.conversation_id != conversation_id
            or message.request_id != request_id
            or message.role != "assistant"
            or message.message_type != MODEL_AUDIT_MESSAGE_TYPE
        ):
            raise ValueError("Model 审计消息必须属于同一 Run、request 和 conversation")

    @staticmethod
    def _require_same_start(message: Message, *, sequence: int) -> None:
        """确保重放的 Model start 没有改写已持久化顺序。"""
        if message.sequence != sequence:
            raise ValueError("重复 Model start 与已持久化 sequence 不一致")
