from __future__ import annotations

from pathlib import Path

import pytest

from sandbox import LogicalRoot, PathMappingSnapshot
from sandbox import DockerSandboxSession
from sandbox.native import linux_arguments
from sandbox.policy import NativePolicy
from tool import ToolContext


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "project"
    source = tmp_path / "agent-source"
    workspace.mkdir()
    (source / "skills").mkdir(parents=True)
    (source / "extension" / "hook").mkdir(parents=True)
    return workspace, source


def test_logical_roots_and_aliases_are_derived_from_source(tmp_path: Path) -> None:
    workspace, source = _tree(tmp_path)
    mapping = PathMappingSnapshot(workspace, source, platform="nt")

    assert mapping.resolve_workspace_path(r"YYWorkspace:\src\main.py") == (
        workspace / "src" / "main.py"
    )
    assert mapping.resolve_agent_source_path(r"YYSkills:\demo\SKILL.md") == (
        source / "skills" / "demo" / "SKILL.md"
    )
    assert mapping.resolve_agent_source_path(r"YYHooks:\demo.py") == (
        source / "extension" / "hook" / "demo.py"
    )

    worktree = tmp_path / "worktree"
    (worktree / "skills").mkdir(parents=True)
    (worktree / "extension" / "hook").mkdir(parents=True)
    switched = mapping.with_agent_source(worktree)
    assert switched.skills_root == worktree.resolve() / "skills"
    assert switched.hooks_root == worktree.resolve() / "extension" / "hook"
    assert switched.resolve_agent_source_path(r"YYHooks:\demo.py").is_relative_to(worktree)
    assert not switched.resolve_agent_source_path(r"YYHooks:\demo.py").is_relative_to(source)


@pytest.mark.parametrize(
    "requested",
    [
        r"..\outside.txt",
        r"YYWorkspace:\..\outside.txt",
        r"C:\Users\person\secret.txt",
        r"\\server\share\secret.txt",
        r"\\?\C:\secret.txt",
        r"~\secret.txt",
        r"$HOME/secret.txt",
        r"%USERPROFILE%\secret.txt",
    ],
)
def test_model_paths_cannot_escape_or_expand_host_locations(
    tmp_path: Path, requested: str,
) -> None:
    workspace, source = _tree(tmp_path)
    mapping = PathMappingSnapshot(workspace, source)
    with pytest.raises((PermissionError, ValueError)):
        mapping.resolve_workspace_path(requested)


def test_symlink_or_reparse_traversal_is_rejected(tmp_path: Path) -> None:
    workspace, source = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating a symlink requires privileges on this host")
    mapping = PathMappingSnapshot(workspace, source)
    with pytest.raises(PermissionError, match="symlink|reparse"):
        mapping.resolve_workspace_path("linked/secret.txt")


def test_symlink_root_is_rejected_instead_of_silently_canonicalized(tmp_path: Path) -> None:
    workspace, source = _tree(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("creating a symlink requires privileges on this host")
    with pytest.raises(ValueError, match="symlink|reparse"):
        PathMappingSnapshot(alias, source)


def test_model_projection_replaces_host_paths_and_tool_context_uses_mapping(
    tmp_path: Path,
) -> None:
    workspace, source = _tree(tmp_path)
    mapping = PathMappingSnapshot(workspace, source, platform="nt")
    context = ToolContext(project_root=workspace, path_mapping=mapping)

    assert context.resolve_workspace_path(r"YYWorkspace:\notes.txt") == workspace / "notes.txt"
    projected = context.sanitize_model_output(
        f"cwd={workspace} source={source / 'extension' / 'hook'}"
    )
    assert str(workspace) not in projected
    assert str(source) not in projected
    assert "YYWorkspace:" in projected
    assert "YYHooks:" in projected


def test_posix_shell_names_are_stable(tmp_path: Path) -> None:
    workspace, source = _tree(tmp_path)
    mapping = PathMappingSnapshot(workspace, source, platform="posix")
    assert mapping.shell_root(LogicalRoot.WORKSPACE) == "/yy/workspace"
    assert mapping.shell_root(LogicalRoot.AGENT_SOURCE) == "/yy/agent-source"
    assert mapping.resolve_workspace_path("/yy/workspace/src/a.py") == workspace / "src" / "a.py"
    translated = mapping.translate_posix_command("cd /yy/workspace && ls /yy/hooks")
    assert "/yy/workspace" not in translated
    assert "/yy/hooks" not in translated
    assert str(workspace) in translated
    assert mapping.translate_posix_command("echo /yy/workspace-old") == "echo /yy/workspace-old"


def test_harness_source_aliases_do_not_expand_beyond_its_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "skills").mkdir(parents=True)
    (worktree / "extension" / "hook").mkdir(parents=True)
    mapping = PathMappingSnapshot(worktree, worktree, platform="posix")

    assert mapping.resolve_workspace_path("/yy/agent-source/Agent/config.py") == (
        worktree / "Agent" / "config.py"
    )
    assert mapping.resolve_workspace_path("/yy/hooks/example.py") == (
        worktree / "extension" / "hook" / "example.py"
    )


def test_runtime_docker_mount_uses_logical_workspace(tmp_path: Path) -> None:
    workspace, _ = _tree(tmp_path)
    (workspace / "skills").mkdir()
    (workspace / "extension" / "hook").mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / ".env").write_text("secret", encoding="utf-8")
    mapping = PathMappingSnapshot(workspace, workspace, platform="posix")
    sandbox = DockerSandboxSession(workspace, path_mapping=mapping)
    arguments = sandbox._docker_run_arguments("test-container")
    joined = "\n".join(arguments)
    assert "target=/yy/workspace" in joined
    assert "target=/yy/agent-source" in joined
    assert "/yy/agent-source/.git" in joined
    assert "target=/yy/agent-source/.env" in joined
    assert sandbox._workspace_target == "/yy/workspace"


def test_runtime_bubblewrap_chdir_and_mount_targets_are_logical(tmp_path: Path) -> None:
    workspace, _ = _tree(tmp_path)
    (workspace / ".env").write_text("secret", encoding="utf-8")
    mapping = PathMappingSnapshot(workspace, workspace, platform="posix")
    arguments = linux_arguments(
        NativePolicy(workspace),
        "bwrap",
        ["/bin/bash", "-c", "pwd"],
        path_mapping=mapping,
    )
    triples = [arguments[index:index + 3] for index in range(len(arguments))]
    assert ["--bind", str(workspace), "/yy/workspace"] in triples
    assert ["--symlink", "workspace", "/yy/agent-source"] in triples
    assert ["--bind", str(workspace), "/yy/agent-source"] not in triples
    assert ["--ro-bind", "/dev/null", "/yy/workspace/.env"] in triples
    assert arguments[arguments.index("--chdir") + 1] == "/yy/workspace"
