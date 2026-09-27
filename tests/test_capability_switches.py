"""Workspace capability switches must change future Runtime catalogs safely."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import tomllib

import pytest
from fastapi.testclient import TestClient

from Agent import AgentRuntime, load_runtime_config
from capability_switches import CapabilityRevisionConflict, load_switches, set_enabled, tool_catalog
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from skill import SkillService
from tool import default_tools


def test_capability_module_is_in_installed_package() -> None:
    manifest = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "capability_switches" in manifest["tool"]["setuptools"]["py-modules"]


def test_tool_switch_is_workspace_scoped_and_revision_checked(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    assert load_switches(first)["revision"] == 0
    state = set_enabled(first, "tools", "calculator", False, 0, skill_names=set())
    assert state["disabled_tools"] == ["calculator"]
    assert state["revision"] == 1
    assert load_switches(second)["disabled_tools"] == []
    assert "calculator" not in default_tools(first).excluding(state["disabled_tools"]).names()
    with pytest.raises(CapabilityRevisionConflict):
        set_enabled(first, "tools", "calculator", True, 0, skill_names=set())
    assert set_enabled(first, "tools", "calculator", True, 1, skill_names=set())["disabled_tools"] == []


def test_skill_switch_filters_runtime_snapshot_but_keeps_management_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_root = workspace / ".yy" / "skills" / "installed" / "example"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example skill\n---\n\nContent\n", encoding="utf-8",
    )
    service = SkillService(tmp_path / "home", workspace)
    assert [item.name for item in service.catalog_snapshot().skills] == ["example"]
    set_enabled(workspace, "skills", "example", False, 0, skill_names={"example"})
    assert [item.name for item in service.catalog()] == ["example"]
    assert service.catalog_snapshot().skills == ()
    assert "example" not in service.catalog_xml()


def test_unknown_tool_and_invalid_settings_fail_closed(tmp_path: Path) -> None:
    assert {item["name"] for item in tool_catalog()} >= {"read_file", "skill_read", "subagent"}
    with pytest.raises(ValueError, match="Unknown"):
        set_enabled(tmp_path, "tools", "invented_tool", False, 0, skill_names=set())
    path = tmp_path / ".yy" / "capabilities.json"
    path.parent.mkdir()
    path.write_text('{"revision": 0, "disabled_tools": "bad", "disabled_skills": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid"):
        load_switches(tmp_path)


def test_gateway_capability_catalog_and_toggle_endpoint() -> None:
    with TemporaryDirectory() as value:
        root = Path(value)
        app = GatewayApplication(load_runtime_config(root))
        project = app.register_project(root)
        headers = {"Authorization": "Bearer test-token"}
        url = f"/api/v1/projects/{project.project_id}/capabilities"
        with TestClient(create_gateway_api(app, access_token="test-token")) as client:
            initial = client.get(url, headers=headers)
            assert initial.status_code == 200
            assert next(item for item in initial.json()["tools"] if item["name"] == "calculator")["enabled"]
            switched = client.patch(
                f"{url}/tools/calculator", headers=headers,
                json={"enabled": False, "expected_revision": 0},
            )
            assert switched.status_code == 200, switched.text
            assert switched.json()["revision"] == 1
            assert not next(item for item in client.get(url, headers=headers).json()["tools"]
                            if item["name"] == "calculator")["enabled"]
            stale = client.patch(
                f"{url}/tools/calculator", headers=headers,
                json={"enabled": True, "expected_revision": 0},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "capability_conflict"


def test_new_agent_runtime_omits_disabled_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_enabled(workspace, "tools", "calculator", False, 0, skill_names=set())
    set_enabled(workspace, "tools", "subagent", False, 1, skill_names=set())
    config = load_runtime_config(tmp_path / "home", workspace_root=workspace)
    runtime = AgentRuntime(
        config, provider=object(), enable_context_processing=False, enable_sandbox=False,
        enable_extensions=False, enable_references=False, enable_paper_library=False,
    )
    try:
        assert runtime.capability_revision == 2
        assert "calculator" not in runtime.tools.names()
        assert "subagent" not in runtime.tools.names()
        assert "read_file" in runtime.tools.names()
    finally:
        asyncio.run(runtime.close())
