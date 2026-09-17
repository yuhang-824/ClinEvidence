"""模型供应商配置数据访问层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ModelProvider
from yuxi.models.providers.builtin import RETIRED_PROVIDER_IDS


async def list_model_providers(db: AsyncSession, *, include_retired: bool = False) -> list[ModelProvider]:
    """获取全部模型供应商配置。"""
    result = await db.execute(
        select(ModelProvider).order_by(ModelProvider.is_enabled.desc(), ModelProvider.provider_id.asc())
    )
    providers = list(result.scalars().all())
    return [
        p
        for p in providers
        if include_retired
        or p.provider_id not in RETIRED_PROVIDER_IDS
        or set(p.capabilities or []) & {"embedding", "rerank"}
    ]


async def get_model_provider(
    db: AsyncSession, provider_id: str, *, include_retired: bool = False
) -> ModelProvider | None:
    """按 provider_id 获取模型供应商配置。"""
    result = await db.execute(select(ModelProvider).where(ModelProvider.provider_id == provider_id))
    provider = result.scalar_one_or_none()
    if (
        provider
        and not include_retired
        and provider_id in RETIRED_PROVIDER_IDS
        and not set(provider.capabilities or []) & {"embedding", "rerank"}
    ):
        return None
    return provider


async def create_model_provider(db: AsyncSession, data: dict) -> ModelProvider:
    """创建模型供应商配置。"""
    provider = ModelProvider(**data)
    db.add(provider)
    await db.flush()
    await db.refresh(provider)
    return provider


async def update_model_provider(db: AsyncSession, provider: ModelProvider, data: dict) -> ModelProvider:
    """更新模型供应商配置。"""
    for key, value in data.items():
        if key != "provider_id":
            setattr(provider, key, value)
    await db.flush()
    await db.refresh(provider)
    return provider


async def delete_model_provider(db: AsyncSession, provider: ModelProvider) -> None:
    """删除模型供应商配置。"""
    await db.delete(provider)
    await db.flush()
