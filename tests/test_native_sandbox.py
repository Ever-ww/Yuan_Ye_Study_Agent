"""Policy, backend parity, cancellation and real platform isolation tests."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from Agent.config import RuntimeConfig
from sandbox import create_sandbox_session, NativeSandboxSession, DockerSandboxSession, SandboxStatus
from sandbox.native import linux_arguments, seatbelt_profile, shell_argv
from sandbox.policy import NativePolicy
from sandbox.scan_cache import WorkspaceScanCache
from sandbox.session import BashUnavailableError, CommandResult, SandboxUnavailableError, SandboxRecoveryRequired
from tool import ToolContext, default_tools


class NativeSandboxTests(unittest.TestCase):
    def test_native_backend_is_lazy_until_first_bash(self):
        async def check(root):
            session = NativeSandboxSession(root, state_root=root)
            execute = AsyncMock(return_value=CommandResult(
                returncode=0, stdout="yy-sandbox-ready\n",
            ))
            with patch.object(session, "_execute_shell", execute):
                await session.start("lazy")
                self.assertEqual(session.status.mode, "os_lazy")
                execute.assert_not_called()
                await session.run_bash("echo actual")
                self.assertEqual(session.status.mode, "os")
                self.assertEqual(execute.await_count, 2)  # first-use probe + command
                await session.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_workspace_scan_cache_reuses_and_invalidates_on_entry_change(self):
        with tempfile.TemporaryDirectory() as value:
            parent = Path(value)
            root = parent / "workspace"
            root.mkdir()
            policy = NativePolicy(root)
            cache = WorkspaceScanCache(root, parent, backend="test")
            original = NativePolicy.protected_paths
            with patch.object(
                NativePolicy,
                "protected_paths",
                autospec=True,
                side_effect=lambda selected: original(selected),
            ) as scan:
                self.assertEqual(cache.protected_paths(policy), ())
                self.assertFalse(cache.last_cache_hit)
                self.assertEqual(cache.protected_paths(policy), ())
                self.assertTrue(cache.last_cache_hit)
                self.assertEqual(scan.call_count, 1)
                (root / "new.py").write_text("pass", encoding="utf-8")
                self.assertEqual(cache.protected_paths(policy), ())
                self.assertFalse(cache.last_cache_hit)
                self.assertEqual(scan.call_count, 2)

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

    @unittest.skipUnless(sys.platform == "win32", "Windows permission preflight")
    def test_acl_preflight_reports_target_without_mutating_acl(self):
        import ctypes
        from sandbox.windows import WinAPI
        with tempfile.TemporaryDirectory() as value:
            api = object.__new__(WinAPI)
            api.kernel = MagicMock()
            api.advapi = MagicMock()
            api.kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
            with self.assertRaisesRegex(SandboxUnavailableError, "READ_CONTROL/WRITE_DAC") as failure:
                api.check_acl_access(Path(value))
            self.assertEqual(failure.exception.reason_code, "windows_acl_permission_denied")
            api.advapi.SetNamedSecurityInfoW.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows permission preflight")
    def test_acl_denied_before_profile_and_lease_does_not_create_unknown_side_effect(self):
        import threading
        from sandbox.windows import AppContainerRunner
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            api = MagicMock()
            api.userenv.DeriveAppContainerSidFromAppContainerName.return_value = 0
            api.check_acl_access.side_effect = SandboxUnavailableError("no WRITE_DAC", reason_code="windows_acl_permission_denied")
            runner = AppContainerRunner(NativePolicy(root), Path(value))
            with patch("sandbox.windows.WinAPI", return_value=api):
                with self.assertRaises(SandboxUnavailableError):
                    runner._run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"], 30, threading.Event())
            api.userenv.CreateAppContainerProfile.assert_not_called()
            api.acl.assert_not_called()
            self.assertEqual(list(runner.leases.iterdir()), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows trace lease")
    def test_appcontainer_permission_lease_is_reused_until_close(self):
        from sandbox.windows import AppContainerRunner

        with tempfile.TemporaryDirectory() as value:
            parent = Path(value)
            root = parent / "workspace"
            root.mkdir()
            api = MagicMock()
            api.userenv.DeriveAppContainerSidFromAppContainerName.return_value = 0
            api.userenv.CreateAppContainerProfile.return_value = 0
            api.userenv.DeleteAppContainerProfile.return_value = 0
            runner = AppContainerRunner(NativePolicy(root), parent)
            argv = [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]
            first, first_sid, _ = runner._ensure_lease(api, argv, (), (root,))
            second, second_sid, _ = runner._ensure_lease(api, argv, (), (root,))
            self.assertEqual(first["name"], second["name"])
            self.assertEqual(api.userenv.CreateAppContainerProfile.call_count, 1)
            self.assertEqual(len(list(runner.leases.glob("*.json"))), 1)
            api.advapi.FreeSid(first_sid)
            api.advapi.FreeSid(second_sid)
            with patch("sandbox.windows.WinAPI", return_value=api):
                runner._close()
            self.assertEqual(api.userenv.DeleteAppContainerProfile.call_count, 1)
            self.assertEqual(list(runner.leases.glob("*.json")), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL boundary plan")
    def test_appcontainer_acl_plan_is_bounded_by_policy_roots_not_workspace_files(self):
        from sandbox.windows import AppContainerRunner

        with tempfile.TemporaryDirectory() as value:
            parent = Path(value)
            root = parent / "workspace"
            root.mkdir()
            source = root / "src"
            source.mkdir()
            for index in range(200):
                (source / f"module-{index}.py").write_text("pass")
            git = root / ".git"
            git.mkdir()
            (git / "config").write_text("secret")
            venv = root / ".venv"
            venv.mkdir()
            (venv / "python.exe").write_text("binary")
            api = MagicMock()
            api.userenv.DeriveAppContainerSidFromAppContainerName.return_value = 0
            api.userenv.CreateAppContainerProfile.return_value = 0
            api.userenv.DeleteAppContainerProfile.return_value = 0
            runner = AppContainerRunner(NativePolicy(root), parent)
            argv = [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]
            record, sid, _ = runner._ensure_lease(
                api, argv, ((git, True), (venv, False)), (root,),
            )
            try:
                mutated = {Path(item) for item in record["paths"]}
                self.assertIn(root, mutated)
                self.assertIn(git, mutated)
                self.assertIn(venv, mutated)
                self.assertNotIn(source / "module-0.py", mutated)
                self.assertLessEqual(api.check_acl_access.call_count, 3)
                calls = {
                    (call.args[0], call.kwargs.get("mode"), call.kwargs.get("permissions"))
                    for call in api.acl.call_args_list
                }
                self.assertIn((git, 3, 0x1F01FF), calls)
                self.assertIn((venv, 3, 0x130156), calls)
            finally:
                api.advapi.FreeSid(sid)
                with patch("sandbox.windows.WinAPI", return_value=api):
                    runner._close()

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

    def test_write_roots_narrow_native_permissions_without_new_approval(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "src"
            tests = root / "tests"
            source.mkdir()
            tests.mkdir()
            (root / ".git").mkdir()
            policy = NativePolicy(root)
            selected = policy.writable_roots(("src",))
            self.assertEqual(selected, (source,))
            args = linux_arguments(
                policy, "bwrap", ["/bin/bash", "-c", "pwd"],
                writable_roots=selected,
            )
            triples = [args[index:index + 3] for index in range(len(args))]
            self.assertIn(["--ro-bind", str(root), str(root)], triples)
            self.assertIn(["--bind", str(source), str(source)], triples)
            self.assertNotIn(["--bind", str(tests), str(tests)], triples)
            profile = seatbelt_profile(
                policy, root / "temp", writable_roots=selected,
            )
            self.assertIn(f'(allow file-write* (subpath {json.dumps(str(source))}))', profile)
            self.assertNotIn(f'(allow file-write* (subpath {json.dumps(str(root))}))', profile)
            with self.assertRaises(ValueError):
                policy.writable_roots((".git",))

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

    def test_uv_cache_hardlinks_are_hidden_not_granted_as_source(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            cache = root / ".uv-cache"
            cache.mkdir()
            venv = root / ".venv"
            venv.mkdir()
            (cache / "package.py").write_text("cached dependency")
            os.link(cache / "package.py", venv / "package.py")
            policy = NativePolicy(root)
            self.assertEqual(dict(policy.protected_paths()), {cache: True, venv: False})
            args = linux_arguments(policy, "bwrap", ["/bin/bash", "-c", "pwd"])
            self.assertIn(["--tmpfs", str(cache)], [args[i:i+2] for i in range(len(args))])
            profile = seatbelt_profile(policy, root / "temp")
            self.assertIn(f'(deny file-read* (subpath {json.dumps(str(cache))}))', profile)
            # A link to that same cache in writable source still fails closed.
            os.link(cache / "package.py", root / "unsafe.py")
            with self.assertRaisesRegex(SandboxUnavailableError, "unsafe.py"):
                policy.protected_paths()

    def test_cargo_build_hardlinks_are_readonly_but_arbitrary_target_is_not_exempt(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            target = root / "target"
            target.mkdir()
            (target / "build-script").write_text("artifact")
            os.link(target / "build-script", target / "build-script-abc")
            with self.assertRaises(SandboxUnavailableError):
                NativePolicy(root).protected_paths()
            (root / "Cargo.toml").write_text("[package]")
            self.assertEqual(dict(NativePolicy(root).protected_paths()), {target: False})

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
