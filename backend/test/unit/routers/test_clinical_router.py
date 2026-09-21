"""患者域路由注册与请求契约单元测试。"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from server.routers.clinical_router import clinical

pytestmark = [pytest.mark.unit]


def test_clinical_routes_registered_with_expected_methods():
    """/api/clinical 路由组暴露患者与就诊的最小读写面;PATCH 不提供身份字段更新。"""
    route_methods = {
        (route.path, method)
        for route in clinical.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert ("/clinical/patients", "GET") in route_methods
    assert ("/clinical/patients", "POST") in route_methods
    assert ("/clinical/patients/{patient_id}", "GET") in route_methods
    assert ("/clinical/patients/{patient_id}", "PATCH") in route_methods
    assert ("/clinical/patients/{patient_id}/encounters", "GET") in route_methods
    assert ("/clinical/patients/{patient_id}/encounters", "POST") in route_methods


def test_patient_update_schema_forbids_identity_fields():
    """患者维护请求只允许脱敏展示字段,禁止携带身份与 Owner 字段。"""
    from server.routers.clinical_router import PatientUpdate

    assert set(PatientUpdate.model_fields) == {"display_code", "status"}
    assert PatientUpdate.model_config.get("extra") == "forbid"


def test_thread_update_schema_has_no_patient_binding_field():
    """会话更新请求不提供 patient_id,绑定不可变在 schema 层生效。"""
    from server.routers.chat_router import ThreadCreate, ThreadUpdate

    assert "patient_id" in ThreadCreate.model_fields
    assert "patient_id" not in ThreadUpdate.model_fields
