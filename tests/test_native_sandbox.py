"""Policy, backend parity, cancellation and real platform isolation tests."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from Agent.config import RuntimeConfig
from sandbox import create_sandbox_session, NativeSandboxSession, DockerSandboxSession, SandboxStatus
from sandbox.native import linux_arguments, seatbelt_profile, shell_argv
from sandbox.policy import NativePolicy
from sandbox.session import BashUnavailableError, CommandResult, SandboxUnavailableError, SandboxRecoveryRequired
from tool import ToolContext, default_tools


class NativeSandboxTests(unittest.TestCase):
    def test_unknown_cleanup_preserves_evidence_instead_of_restore(self):
        async def check(root):
            session = NativeSandboxSession(root)
            status = SandboxStatus(mode="os", bash_available=True, message="test")
            async def execute(*args):
                (root / "evidence.txt").write_text("uncertain")
                raise SandboxRecoveryRequired("uncertain child")
            with patch.object(session, "_start_backend", AsyncMock(return_value=status)), patch.object(session, "_execute_shell", execute):
                await session.start("uncertain")
                with self.assertRaises(SandboxRecoveryRequired):
                    await session.run_bash("execute")
                self.assertEqual((root / "evidence.txt").read_text(), "uncertain")
                self.assertFalse(session.bash_available)
                await session.close()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_read_root_cannot_reopen_protected_workspace_paths(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            with self.assertRaises(SandboxUnavailableError):
                NativePolicy(root, (root.parent,)).validate()

    def test_factory_defaults_to_os_and_docker_requires_selection(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = RuntimeConfig(agent_root=root, workspace_root=root)
            self.assertIsInstance(create_sandbox_session(config), NativeSandboxSession)
            self.assertIsInstance(create_sandbox_session(config.model_copy(update={"sandbox_backend": "docker"})), DockerSandboxSession)
            with self.assertRaises(ValueError):
                RuntimeConfig(agent_root=root, workspace_root=root, sandbox_backend="host")

    def test_shell_argument_is_not_reparsed_by_host(self):
        command = 'echo "a b"; echo $TOKEN'
        self.assertEqual(shell_argv(command, "/bin/bash", "linux")[-1], command)
        self.assertEqual(shell_argv(command, "C:/Windows/powershell.exe", "win32")[-2:], ["-Command", command])

    def test_os_status_exposes_bash_without_new_approval_semantics(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            sandbox = NativeSandboxSession(root)
            sandbox._status = SandboxStatus(mode="os", bash_available=True, message="tested")
            registry = default_tools(root)
            self.assertTrue(registry.is_available("bash", ToolContext(project_root=root, sandbox=sandbox)))
            sandbox._status = SandboxStatus(mode="checkpoint_only", bash_available=False, message="unavailable")
            self.assertFalse(registry.is_available("bash", ToolContext(project_root=root, sandbox=sandbox)))

    def test_bwrap_policy_is_offline_and_no_host_home(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".git").mkdir()
            (root / ".yy").mkdir()
            (root / ".env").write_text("secret")
            args = linux_arguments(NativePolicy(root), "bwrap", ["/bin/bash", "-c", "pwd"])
            self.assertIn("--unshare-net", args)
            self.assertIn("--assert-userns-disabled", args)
            self.assertNotIn("--share-net", args)
            self.assertNotIn(str(Path.home()), args)
            self.assertIn(["--ro-bind", "/dev/null", str(root / ".env")], [args[i:i+3] for i in range(len(args))])
            self.assertIn(["--chmod", "000", str(root / ".git")], [args[i:i+3] for i in range(len(args))])

    def test_seatbelt_profile_is_deny_by_default_and_escapes_paths(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".git").mkdir()
            (root / ".venv").mkdir()
            profile = seatbelt_profile(NativePolicy(root), root / "temp")
            self.assertIn("(deny default)", profile)
            self.assertIn("(deny network*)", profile)
            self.assertIn("(deny file-write*", profile)
            self.assertNotIn('(allow file-read*)', profile)

    def test_aliases_and_special_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "a").write_text("data")
            os.link(root / "a", root / "b")
            with self.assertRaises(SandboxUnavailableError):
                NativePolicy(root).protected_paths()

    def test_unavailable_os_never_launches_docker_or_host(self):
        async def check(root):
            session = NativeSandboxSession(root)
            with patch.object(session, "_start_backend", AsyncMock(side_effect=SandboxUnavailableError("unavailable", reason_code="test"))), \
                 patch.object(session, "_execute_shell", AsyncMock()) as execute:
                await session.start("no-os")
                self.assertEqual(session.status.mode, "checkpoint_only")
                with self.assertRaises(BashUnavailableError):
                    await session.run_bash("echo must-not-run")
                execute.assert_not_called()
                await session.close()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_shared_checkpoint_failure_restore_and_cancel_propagation(self):
        async def check(root):
            session = NativeSandboxSession(root)
            status = SandboxStatus(mode="os", bash_available=True, message="test")
            async def execute(command, timeout):
                (root / "new.txt").write_text(command)
                if command == "cancel":
                    raise asyncio.CancelledError
                return CommandResult(returncode=1 if command == "fail" else 0)
            with patch.object(session, "_start_backend", AsyncMock(return_value=status)), patch.object(session, "_execute_shell", execute):
                await session.start("test")
                await session.run_bash("success")
                with self.assertRaises(RuntimeError):
                    await session.run_bash("fail")
                self.assertEqual((root / "new.txt").read_text(), "success")
                with self.assertRaises(asyncio.CancelledError):
                    await session.run_bash("cancel")
                self.assertEqual((root / "new.txt").read_text(), "success")
                self.assertEqual(len(session.list_checkpoints()), 2)
                await session.close()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))


@unittest.skipUnless(os.environ.get("YY_RUN_NATIVE_SANDBOX_TESTS") == "1", "Opt in to real OS process/ACL integration")
class NativeIntegrationTests(unittest.TestCase):
    def test_background_children_are_stopped_before_cancel_and_timeout_restore(self):
        async def check(parent):
            root = parent / "workspace"
            root.mkdir()
            session = NativeSandboxSession(root, state_root=parent)
            try:
                await session.start("process-tree")
                self.assertTrue(session.bash_available, session.status.message)
                if sys.platform == "win32":
                    target = str(root / "zombie.txt").replace("'", "''")
                    code = f"Start-Sleep -Seconds 3; [IO.File]::WriteAllText('{target}', 'bad')"
                    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
                    command = ("$ErrorActionPreference='Stop'; Start-Process -FilePath "
                               f"'{session.shell}' -ArgumentList '-NoProfile -NonInteractive -EncodedCommand {encoded}' "
                               "-NoNewWindow -PassThru | Out-Null; Set-Content ready.txt ready; ")
                    sleeping = "Start-Sleep -Seconds 30"
                else:
                    command = "(sleep 3; echo bad > zombie.txt) & echo ready > ready.txt; "
                    sleeping = "sleep 30"
                task = asyncio.create_task(session.run_bash(command + sleeping, 30))
                try:
                    for _ in range(300):
                        if (root / "ready.txt").exists() or task.done():
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue((root / "ready.txt").exists(), "Child must actually start")
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                await asyncio.sleep(3.5)
                self.assertFalse((root / "zombie.txt").exists())
                self.assertFalse((root / "ready.txt").exists())
                with self.assertRaises((TimeoutError, RuntimeError)):
                    await session.run_bash(sleeping, 1)
                self.assertTrue(session.bash_available)
                # Normal completion also cannot leave a writer running behind us.
                await session.run_bash(command + "echo done")
                await asyncio.sleep(3.5)
                self.assertFalse((root / "zombie.txt").exists())
                if sys.platform == "win32":
                    self.assertEqual(list((parent / ".yy/sandbox/native-leases").glob("*.json")), [])
            finally:
                await session.close()
        with tempfile.TemporaryDirectory(prefix="yy-native-tree-") as value:
            asyncio.run(check(Path(value)))

    def test_workspace_write_outside_and_metadata_denied_and_network_off(self):
        async def check(parent):
            root = parent / "workspace"
            root.mkdir()
            (root / ".git").mkdir()
            (root / ".git/protected").write_text("original")
            (root / ".env").write_text("secret")
            (root / ".venv").mkdir()
            (root / ".venv/existing").write_text("readonly")
            secret = parent / "outside-secret"
            secret.write_text("outside-secret")
            session = NativeSandboxSession(root, state_root=parent)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            try:
                await session.start("native-real")
                self.assertTrue(session.bash_available, session.status.message)
                if sys.platform == "win32":
                    outside = str(secret).replace("'", "''")
                    command = (
                        "$ErrorActionPreference='Stop'; Set-Content allowed.txt ok; "
                        f"try {{ Get-Content '{outside}'; throw 'outside-read-allowed' }} catch {{ if ($_.Exception.Message -eq 'outside-read-allowed') {{ throw }} }}; "
                        f"try {{ Set-Content '{outside}' bad; throw 'outside-write-allowed' }} catch {{ if ($_.Exception.Message -eq 'outside-write-allowed') {{ throw }} }}; "
                        "try { Set-Content .git/protected bad; throw 'metadata-write-allowed' } catch { if ($_.Exception.Message -eq 'metadata-write-allowed') { throw } }; "
                        "try { Get-Content .env; throw 'secret-read-allowed' } catch { if ($_.Exception.Message -eq 'secret-read-allowed') { throw } }; "
                        "try { Set-Content .venv/existing bad; throw 'toolchain-write-allowed' } catch { if ($_.Exception.Message -eq 'toolchain-write-allowed') { throw } }; "
                        "$a=Get-Acl .; $s=New-Object Security.Principal.SecurityIdentifier 'S-1-15-2-1'; "
                        "$r=New-Object Security.AccessControl.FileSystemAccessRule($s,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); $a.AddAccessRule($r); "
                        "try { Set-Acl . $a; throw 'acl-escalation-allowed' } catch { if ($_.Exception.Message -eq 'acl-escalation-allowed') { throw } }; "
                        f"try {{ $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',{port}); throw 'network-allowed' }} catch {{ if ($_.Exception.Message -eq 'network-allowed') {{ throw }} }}; "
                        "Write-Output isolation-ok"
                    )
                else:
                    import shlex
                    target = shlex.quote(str(secret))
                    # Linux masks sensitive files with /dev/null; macOS denies
                    # the read. Both must hide the original bytes.
                    command = f"echo ok > allowed.txt; if cat {target}; then exit 21; fi; if echo bad > {target}; then exit 22; fi; if echo bad > .git/protected; then exit 23; fi; if echo net > /dev/tcp/127.0.0.1/{port}; then exit 24; fi; if test -n \"$(cat .env 2>/dev/null)\"; then exit 25; fi; if echo bad > .venv/existing; then exit 26; fi; echo isolation-ok"
                result = await session.run_bash(command)
                self.assertIn("isolation-ok", result.output)
                self.assertTrue((root / "allowed.txt").is_file())
                self.assertEqual(secret.read_text(), "outside-secret")
                self.assertEqual((root / ".git/protected").read_text(), "original")
            finally:
                listener.close()
                await session.close()
        with tempfile.TemporaryDirectory(prefix="yy-native-real-") as value:
            asyncio.run(check(Path(value)))
