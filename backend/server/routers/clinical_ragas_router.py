"""患者问答 RAGAS 评估的 HTTP 适配层。"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from server.utils.auth_middleware import get_required_user
from yuxi.services import clinical_ragas_service as service
from yuxi.storage.postgres.models_business import User

clinical_ragas = APIRouter(prefix="/clinical/evaluation", tags=["clinical-evaluation"])
MAX_FILE_BYTES = 10 * 1024 * 1024


class StartRunRequest(BaseModel):
    """选择 Agent 与独立评判模型。"""

    dataset_id: str = Field(min_length=1)
    agent_slug: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)


async def _read_upload(file: UploadFile | None) -> bytes | None:
    """限制每份输入大小，避免将任意文件直接持久化。"""
    if file is None:
        return None
    if not (file.filename or "").lower().endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="仅支持 JSONL 文件")
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="单个文件不能超过 10 MB")
    return content


@clinical_ragas.get("/options")
async def options(current_user: User = Depends(get_required_user)):
    """返回评估可选配置。"""
    return await service.get_options(current_user)


@clinical_ragas.post("/datasets")
async def upload_dataset(
    name: str = Form(..., min_length=1, max_length=100),
    samples: UploadFile = File(...),
    metadata: UploadFile | None = File(None),
    scope_map: UploadFile | None = File(None),
    current_user: User = Depends(get_required_user),
):
    """上传人工指定的测试题、来源元数据和患者映射。"""
    try:
        return await service.upload_dataset(
            uid=str(current_user.uid),
            name=name,
            samples=await _read_upload(samples),
            metadata=await _read_upload(metadata),
            scope_map=await _read_upload(scope_map),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@clinical_ragas.get("/datasets")
async def list_datasets(current_user: User = Depends(get_required_user)):
    """列出本人测试集。"""
    return await service.list_datasets(str(current_user.uid))


@clinical_ragas.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, current_user: User = Depends(get_required_user)):
    """读取本人测试集详情。"""
    return await service.get_dataset(str(current_user.uid), dataset_id)


@clinical_ragas.post("/runs")
async def start_run(payload: StartRunRequest, current_user: User = Depends(get_required_user)):
    """持久提交一次后台评估。"""
    try:
        return await service.start_run(uid=str(current_user.uid), **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@clinical_ragas.get("/runs")
async def list_runs(current_user: User = Depends(get_required_user)):
    """列出本人最近评估。"""
    return await service.list_runs(str(current_user.uid))


@clinical_ragas.get("/runs/{run_id}")
async def get_run(run_id: str, current_user: User = Depends(get_required_user)):
    """读取本人逐题指标与错误。"""
    return await service.get_run(str(current_user.uid), run_id)
