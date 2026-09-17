"""显式启用的本地 PDF 冒烟：真实 HTTP、worker、PG、MinIO、Milvus，Embedding 使用测试替身。"""

import asyncio
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from yuxi.agents.skills.repository import SkillRepository
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Department, Skill, User
from yuxi.storage.postgres.models_knowledge import KnowledgeChunk, KnowledgeFile
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration,
              pytest.mark.skipif(os.getenv("CLINEVIDENCE_SCOPE_SMOKE") != "1", reason="显式启用本地临时测试资源")]


class EmbeddingStub(BaseHTTPRequestHandler):
    """提供确定性向量，只验证协议与存储链路，不评估语义质量。"""

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        inputs = data['input']
        if isinstance(inputs, str):
            inputs = [inputs]
        body = json.dumps({'data': [{'index': i, 'embedding': [1.0] + [0.0] * 7}
                                   for i in range(len(inputs))], 'usage': {'total_tokens': len(inputs)}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


async def test_pdf_upload_parse_index_retrieve_delete_without_graph(tmp_path):
    """无图谱服务时，合成 PDF 可经真实 worker 入库并回读来源。"""
    from test.unit.knowledge.test_parser_facade import _build_pdf

    pg_manager.initialize()
    suffix = uuid.uuid4().hex[:12]
    uid = f'pytest_scope_{suffix}'
    password = uuid.uuid4().hex
    provider_id = f'pytest-scope-{suffix}'
    kb_id = None
    provider_created = False
    server = ThreadingHTTPServer(('0.0.0.0', 0), EmbeddingStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    async with pg_manager.get_async_session_context() as session:
        department = Department(name=uid)
        session.add(department)
        await session.flush()
        department_id = department.id
        user = User(username=uid, uid=uid, password_hash=AuthUtils.hash_password(password), role='superadmin',
                    department_id=department_id)
        session.add(user)
        await session.commit()
    async with httpx.AsyncClient(base_url=os.getenv('TEST_BASE_URL', 'http://localhost:5050'), timeout=60) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            assert response.is_success, f'{method} {path}: {response.status_code} {response.text}'
            return response.json()

        try:
            login = await request('POST', '/api/auth/token', data={'username': uid, 'password': password})
            client.headers['Authorization'] = f"Bearer {login['access_token']}"
            for path in ['/api/graph/list', '/api/knowledge/databases/missing/graph-build/status']:
                assert (await client.get(path)).status_code == 404
            assert (await client.post('/api/knowledge/files/fetch-url', json={'url': 'https://example.com'})).status_code == 404
            types = await request('GET', '/api/knowledge/types')
            assert set(types['kb_types']) == {'milvus'}
            async with pg_manager.get_async_session_context() as session:
                repo = SkillRepository(session)
                for slug in ('deep-research', 'mysql-reporter'):
                    # 在当前事务内准备缺失的历史行；退出时回滚，不更改已有记录。
                    present = (await session.execute(select(Skill).where(Skill.slug == slug))).scalar_one_or_none()
                    if present is None:
                        session.add(Skill(slug=slug, name=slug, description='Synthetic retired skill',
                                          source_type='builtin', dir_path='/synthetic-unused', share_config={}))
                        await session.flush()
                    else:
                        assert present.source_type == 'builtin'
                    assert await repo.get_by_slug(slug) is None
                    assert await repo.exists_slug(slug) is True
                await session.rollback()
            await request('POST', '/api/system/model-providers', json={
                'provider_id': provider_id, 'display_name': 'Temporary scope smoke', 'provider_type': 'openai',
                'base_url': f'http://api:{server.server_port}/v1',
                'embedding_base_url': f'http://api:{server.server_port}/v1/embeddings', 'api_key': 'local-test-only',
                'capabilities': ['embedding'], 'enabled_models': [{'id': 'stub', 'type': 'embedding', 'dimension': 8}],
            })
            provider_created = True
            database = await request('POST', '/api/knowledge/databases', json={
                'database_name': uid, 'description': 'Synthetic local PDF smoke',
                'embedding_model_spec': f'{provider_id}:stub', 'kb_type': 'milvus',
            })
            kb_id = database['kb_id']
            pdf = Path(tmp_path) / 'scope.pdf'
            _build_pdf(pdf, ['ClinEvidence synthetic source ALPHA evidence.', 'Second source page BETA evidence.'])
            upload = await request('POST', f'/api/knowledge/files/upload?kb_id={kb_id}',
                                   files={'file': ('scope.pdf', pdf.read_bytes(), 'application/pdf')})
            submitted = await request('POST', f'/api/knowledge/databases/{kb_id}/documents', json={
                'items': [upload['file_path']], 'params': {'ocr_engine': 'disable', 'auto_index': True,
                    'content_hashes': {upload['file_path']: upload['content_hash']},
                    'file_sizes': {upload['file_path']: upload['size']}},
            })
            for _ in range(120):
                task = (await request('GET', f"/api/tasks/{submitted['task_id']}"))['task']
                if task['status'] in {'success', 'failed', 'cancelled'}:
                    break
                await asyncio.sleep(1)
            assert task['status'] == 'success', task
            async with pg_manager.get_async_session_context() as session:
                files = list((await session.execute(select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id))).scalars())
                chunks = list((await session.execute(select(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb_id))).scalars())
                assert len(files) == 1 and files[0].status == 'indexed'
                assert chunks and 'ALPHA' in '\n'.join(chunk.content for chunk in chunks)
                file_id = files[0].file_id
            # Milvus 跨 API/worker 的可见性存在延迟，以最终检索结果为准。
            for _ in range(30):
                result = await request('POST', f'/api/knowledge/databases/{kb_id}/query', json={'query': 'ALPHA', 'meta': {}})
                if 'ALPHA' in json.dumps(result):
                    break
                await asyncio.sleep(1)
            assert result['status'] == 'success' and 'ALPHA' in json.dumps(result)
            original = await client.get(f'/api/knowledge/databases/{kb_id}/documents/{file_id}/download')
            assert original.status_code == 200 and original.content == pdf.read_bytes()
            await request('DELETE', f'/api/knowledge/databases/{kb_id}')
            async with pg_manager.get_async_session_context() as session:
                assert not (await session.execute(select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id))).scalars().all()
                assert not (await session.execute(select(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb_id))).scalars().all()
            kb_id = None
        finally:
            if kb_id:
                await request('DELETE', f'/api/knowledge/databases/{kb_id}')
            if provider_created:
                await request('DELETE', f'/api/system/model-providers/{provider_id}')
            async with pg_manager.get_async_session_context() as session:
                attached_user = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
                await UserRepository(session).delete_for_admin(attached_user)
                attached_user.department_id = None
                await session.execute(delete(Department).where(Department.id == department_id))
                await session.commit()
            async with pg_manager.get_async_session_context() as session:
                deleted_user = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
                assert deleted_user.is_deleted == 1 and deleted_user.password_hash == "DELETED"
            server.shutdown()
            server.server_close()
