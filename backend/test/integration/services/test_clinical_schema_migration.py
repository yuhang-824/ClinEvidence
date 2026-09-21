"""患者域 Schema 迁移在真实 PostgreSQL 上的集成测试:新库建表、v7 升级收敛与不可变绑定。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError

from yuxi.storage.postgres.manager import PostgresManager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PATIENT_TABLES = (
    "patients",
    "patient_access_grants",
    "encounters",
    "patient_import_batches",
    "patient_documents",
    "patient_document_versions",
    "patient_document_revisions",
    "patient_chunks",
    "patient_snapshots",
    "patient_snapshot_members",
)


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 测试没有 HTTP 资源需要清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 测试没有 Sandbox 资源需要清理。"""
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


async def _create_isolated_manager(prefix: str):
    """创建位于独立 PostgreSQL Schema 的 manager 与清理句柄。"""
    schema = f"{prefix}_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    return schema, admin_engine, scoped_engine, _scoped_manager(scoped_engine)


async def _drop_isolated_schema(schema: str, admin_engine, scoped_engine) -> None:
    """释放隔离 Schema 及其 engine。"""
    await scoped_engine.dispose()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await admin_engine.dispose()


async def test_patient_tables_created_with_conversation_binding_idempotently() -> None:
    """新库一次建齐患者域表与 conversations.patient_id 绑定,重复收敛幂等。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_clinical_schema")
    try:
        await manager.create_business_tables()
        await manager.ensure_business_schema()
        await manager.ensure_business_schema()

        async with scoped_engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :schema"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'conversations'::regclass AND contype = 'f'"
                        )
                    )
                ).scalars()
            )
        assert set(PATIENT_TABLES) <= tables
        assert "fk_conversations_patient_id_patients" in constraints
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)


async def test_v7_business_database_converges_patient_binding() -> None:
    """模拟 business v7 旧库:升级路径必须补建患者域表并恢复 conversations 绑定列与外键。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_clinical_upgrade")
    try:
        await manager.create_business_tables()
        async with scoped_engine.begin() as connection:
            for table in PATIENT_TABLES:
                await connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            await connection.execute(text("ALTER TABLE conversations DROP COLUMN IF EXISTS patient_id"))

        await manager.create_business_tables()
        await manager.ensure_business_schema()

        async with scoped_engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :schema"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'conversations'"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            fk_exists = (
                await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'fk_conversations_patient_id_patients' "
                        "AND conrelid = 'conversations'::regclass)"
                    )
                )
            ).scalar()
        assert set(PATIENT_TABLES) <= tables
        assert "patient_id" in columns
        assert fk_exists is True
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)


async def test_patient_binding_fk_restricts_delete_and_requires_existing_patient() -> None:
    """患者绑定外键:引用必须存在;RESTRICT 阻止删除被绑定患者行静默解绑。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_clinical_fk")
    try:
        await manager.create_business_tables()
        await manager.ensure_business_schema()
        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)

        async with factory() as session:
            from yuxi.storage.postgres.models_business import Conversation, Project, User
            from yuxi.storage.postgres.models_clinical import Patient

            user = User(uid="fk-doctor", username="fk-doctor", password_hash="x", role="user")
            session.add(user)
            await session.flush()
            project = Project(
                id="fk-proj",
                uid="fk-doctor",
                selection_status="implicit",
                workdir_path="workspaces/fk-doctor/proj",
                directory_mode="managed",
            )
            session.add(project)
            patient = Patient(
                id="fk-patient",
                owner_uid="fk-doctor",
                display_code="P-FKTEST01",
                status="active",
            )
            session.add(patient)
            await session.flush()
            session.add(
                Conversation(
                    thread_id="fk-thread",
                    uid="fk-doctor",
                    agent_id="clinical",
                    project_id="fk-proj",
                    patient_id="fk-patient",
                )
            )
            await session.commit()

        async with factory() as session:
            missing = Conversation(
                thread_id="fk-thread-missing",
                uid="fk-doctor",
                agent_id="clinical",
                project_id="fk-proj",
                patient_id="no-such-patient",
            )
            session.add(missing)
            with pytest.raises(IntegrityError):
                await session.commit()

        async with factory() as session:
            bound = await session.get(Patient, "fk-patient")
            await session.delete(bound)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)
