"""审核请求体的字段契约：前端发出的键名必须与模型字段一致。"""

from server.routers.knowledge_router import DocumentReviewInput


def test_review_input_fields_are_pinned():
    """字段名是前后端共享的契约，改名必须同时改前端并更新此断言。"""
    assert set(DocumentReviewInput.model_fields) == {
        "action",
        "version",
        "content",
        "boundaries",
        "structure",
        "base_saved_at",
    }


def test_base_saved_at_reaches_the_router():
    """草稿时间戳是原地更新时的并发守卫，解析后必须真的带上。"""
    payload = DocumentReviewInput.model_validate(
        {"action": "save", "version": 1, "base_saved_at": "2026-09-20T10:00:00"}
    )

    assert payload.base_saved_at == "2026-09-20T10:00:00"


def test_camel_case_key_is_silently_dropped():
    """未知键被忽略而不是报错：前端写错键名时守卫会静默失效，故前端必须发 snake_case。"""
    payload = DocumentReviewInput.model_validate({"action": "save", "version": 1, "baseSavedAt": "2026-09-20T10:00:00"})

    assert payload.base_saved_at is None
