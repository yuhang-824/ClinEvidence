from yuxi.knowledge.implementations.milvus import _retrieval_config_options


def test_local_retrieval_keeps_vector_and_rerank_without_graph():
    """本地知识库暴露向量与重排参数，不再暴露图谱能力。"""
    fields = _retrieval_config_options()
    keys = {item["key"] for item in fields}
    assert "use_reranker" in keys
    assert "use_graph_retrieval" not in keys
    assert not any(key.startswith("graph_") for key in keys)
