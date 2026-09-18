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
from sqlalchemy import delete, select, update

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
    from test.unit.knowledge.test_pdf_layout import build_layout_pdf

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
            build_layout_pdf(pdf)
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
                assert len(files) == 1 and files[0].status == 'parsed'
                assert not chunks
                file_id = files[0].file_id
            review_url = f'/api/knowledge/databases/{kb_id}/documents/{file_id}/review'
            review = await request('GET', review_url)
            assert review['revisions'][0]['raw_content']
            assert review['revisions'][0]['approved_at'] is None
            async with pg_manager.get_async_session_context() as session:
                await session.execute(update(User).where(User.id == user.id).values(role='user'))
            try:
                denied = await client.post(review_url, json={'action': 'approve', 'version': 1})
                assert denied.status_code == 403
            finally:
                async with pg_manager.get_async_session_context() as session:
                    await session.execute(update(User).where(User.id == user.id).values(role='superadmin'))
            # 直接索引入口必须拒绝未审核稿，且不产生片段。
            blocked = await request('POST', f'/api/knowledge/databases/{kb_id}/documents/index', json={'file_ids': [file_id], 'params': {}})
            for _ in range(120):
                blocked_task = (await request('GET', f"/api/tasks/{blocked['task_id']}"))['task']
                if blocked_task['status'] in {'success', 'failed', 'cancelled'}:
                    break
                await asyncio.sleep(1)
            async with pg_manager.get_async_session_context() as session:
                assert not list(await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.file_id == file_id)))
            edited = review['revisions'][0]['content'] + '\n\nAUDITED CONTENT MARKER'
            saved = await request('POST', review_url, json={'action': 'save', 'version': 1, 'content': edited})
            assert saved['revisions'][-1]['version'] == 2
            assert (await client.post(review_url, json={'action': 'approve', 'version': 1})).status_code == 409
            assert (await client.post(review_url, json={'action': 'save', 'version': 2, 'content': ' '})).status_code == 400
            assert (await client.get(f'/api/knowledge/databases/{kb_id}/documents/missing/review')).status_code == 404
            approved = await request('POST', review_url, json={'action': 'approve', 'version': 2})
            assert approved['revisions'][-1]['approved_at']
            preview_url = review_url.removesuffix('/review') + '/chunk-preview'
            assert (await client.post(preview_url, json={'version': 1})).status_code == 409
            assert (await client.post(preview_url, json={'version': 2, 'chunk_token_num': 0})).status_code == 422
            preview = await request('POST', preview_url, json={'version': 2, 'chunk_token_num': 64})
            assert preview['chunks'] and preview['params']['review_version'] == 2
            for bad in ([0], [len(edited)], [5, 3], [True], [3.5]):
                response = await client.post(review_url, json={'action': 'boundaries', 'version': 2, 'boundaries': bad})
                assert response.status_code in {400, 422}, response.text
            cut = edited.index('AUDITED CONTENT MARKER')
            repaired = await request('POST', review_url, json={'action': 'boundaries', 'version': 2, 'boundaries': [cut]})
            assert repaired['revisions'][-1]['version'] == 3
            assert repaired['revisions'][-1]['content'] == edited
            assert repaired['revisions'][-1]['approved_at'] is None
            reloaded = await request('GET', review_url)
            assert reloaded['revisions'][-1]['report']['chunk_boundaries'] == [cut]
            assert (await client.post(review_url, json={'action': 'boundaries', 'version': 2, 'boundaries': []})).status_code == 409
            await request('POST', review_url, json={'action': 'approve', 'version': 3})
            preview = await request('POST', preview_url, json={'version': 3, 'chunk_token_num': 64})
            assert len(preview['chunks']) == 2
            assert preview['chunks'][-1]['content'] == 'AUDITED CONTENT MARKER'
            # 原文件列表等非 mixed 入口也必须执行审核稿的人工边界。
            preview['params']['chunk_preset_id'] = 'general'
            indexed = await request('POST', f'/api/knowledge/databases/{kb_id}/documents/index', json={'file_ids': [file_id], 'params': preview['params']})
            for _ in range(120):
                task = (await request('GET', f"/api/tasks/{indexed['task_id']}"))['task']
                if task['status'] in {'success', 'failed', 'cancelled'}:
                    break
                await asyncio.sleep(1)
            async with pg_manager.get_async_session_context() as session:
                file = await session.scalar(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))
                chunks = list(await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.file_id == file_id)))
                assert file.status == 'indexed', task
                assert 'AUDITED CONTENT MARKER' in '\n'.join(c.content for c in chunks)
                chunks.sort(key=lambda c: c.chunk_index)
                assert [c.content for c in chunks] == [c['content'] for c in preview['chunks']]
                assert all(c.source_metadata['revision'] == 3 for c in chunks)
            source_url = review_url.removesuffix('/review') + f'/chunks/{chunks[0].chunk_id}/source?version=3'
            source_before = await request('GET', source_url)
            assert '\n\n'.join(s['text'].strip() for s in source_before['excerpts']) == chunks[0].content
            assert (await client.get(source_url.replace('version=3', 'version=1'))).status_code == 409
            assert (await client.get(source_url.replace(f'/documents/{file_id}/', '/documents/missing/'))).status_code == 404
            assert (await client.get(source_url.replace(kb_id, 'missing-kb'))).status_code in {403, 404}
            external = f'/api/knowledge/databases/external/{kb_id}/files/{file_id}'
            opened = await request('GET', external + '/open')
            assert 'AUDITED CONTENT MARKER' in json.dumps(opened)
            found = await request('POST', external + '/find', json={'patterns': ['AUDITED CONTENT MARKER']})
            assert 'AUDITED CONTENT MARKER' in json.dumps(found)
            await request('POST', review_url, json={'action': 'save', 'version': 3, 'content': 'UNPUBLISHED DRAFT'})
            pending = await request('GET', review_url)
            assert 'chunk_boundaries' not in pending['revisions'][-1]['report']
            assert await request('GET', source_url) == source_before
            opened = await request('GET', external + '/open')
            assert 'AUDITED CONTENT MARKER' in json.dumps(opened) and 'UNPUBLISHED DRAFT' not in json.dumps(opened)
            parsed = await request('GET', f'/api/knowledge/databases/{kb_id}/documents/{file_id}/content')
            markdown = parsed['content']
            assert markdown.index('LEFT LAST') < markdown.index('RIGHT FIRST')
            assert '| Name | BETA |' in markdown
            assert 'Date: 2026-01-01' in markdown
            # Milvus 跨 API/worker 的可见性存在延迟，以最终检索结果为准。
            for _ in range(30):
                result = await request('POST', f'/api/knowledge/databases/{kb_id}/query', json={'query': 'ALPHA', 'meta': {}})
                if 'ALPHA' in json.dumps(result):
                    break
                await asyncio.sleep(1)
            assert result['status'] == 'success' and 'ALPHA' in json.dumps(result)
            assert any(c['metadata'].get('source_metadata', {}).get('revision') == 3 for c in result['result'])
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
