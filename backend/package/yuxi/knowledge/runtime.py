"""知识库运行时单例。"""

import os

from yuxi.config import get_runtime_dir
from yuxi.knowledge.factory import KnowledgeBaseFactory
from yuxi.knowledge.implementations.milvus import MilvusKB
from yuxi.knowledge.manager import KnowledgeBaseManager

KnowledgeBaseFactory.register(MilvusKB)

knowledge_base = KnowledgeBaseManager(os.path.join(get_runtime_dir(), "knowledge_base_data"))
