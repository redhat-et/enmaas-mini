#!/usr/bin/env python3
"""Offline regression checks for test-bundle boundaries and real CLI fixtures."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from contextlib import redirect_stdout

import run as runner
import harness_provider as harness
import host
from integration import ran_tests
import integration


class EvidenceTest(unittest.TestCase):
    def test_interruption_stops_child_tools_before_restoring_gateway(self):
        with patch.object(integration.subprocess.Popen, "communicate", side_effect=[KeyboardInterrupt(), ("", "")]), \
             patch.object(integration.os, "killpg", wraps=os.killpg) as killed:
            with self.assertRaises(KeyboardInterrupt):
                integration.run_captured([sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)
            killed.assert_called()

    def test_installed_model_preserves_legacy_hosts_and_rejects_invalid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model"
            self.assertEqual(host.installed_model(path), "qwen3-8b")
            path.write_text("qwen3.8-27b-int4\n")
            self.assertEqual(host.installed_model(path), "qwen3.8-27b-int4")
            path.write_text("unknown\n")
            with self.assertRaises(ValueError):
                host.installed_model(path)

    def test_real_gateway_uses_installed_model_but_mock_keeps_fixture_alias(self):
        for real, expected in ((True, "qwen3.8-27b-int4"), (False, "qwen3-8b")):
            with patch.object(integration, "installed_model", return_value="qwen3.8-27b-int4"):
                gateway = integration.InstalledGateway(Namespace(scenario="all-in-one", provider="vllm", real=real))
            self.assertEqual(gateway.model, expected)

    def test_cpu_harness_has_a_longer_bounded_deadline_without_affecting_mocks(self):
        with tempfile.TemporaryDirectory() as directory:
            mode = Path(directory) / "mode"
            for inference, expected in (("cpu", 3600), ("gpu", 1800)):
                mode.write_text(inference + "\n")
                self.assertEqual(integration.harness_timeout(True, mode), expected)
            mode.unlink()
            self.assertEqual(integration.harness_timeout(False, mode), 180)
            mode.write_text("invalid\n")
            with self.assertRaises(ValueError):
                integration.harness_timeout(True, mode)

    def test_runtime_record_keeps_versions_and_hashes_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inference, gateway = root / "vllm", root / "praxis"
            inference.mkdir()
            gateway.mkdir()
            (inference / "mode").write_text("cpu\n")
            (inference / "chat-template.jinja").write_text("template")
            (gateway / "shared-gateway.yaml").write_text("private configuration")
            containers = [{"Name": name, "ImageName": name + "@sha256:abc", "Image": "image-id",
                           "Config": {"Env": ["PRIVATE_SECRET=must-not-record"],
                                      "Labels": {"org.opencontainers.image.revision": "revision"},
                                      "Cmd": ["model", "--revision", "model-revision",
                                              "--chat-template", "/etc/vllm-chat-template.jinja",
                                              "--default-chat-template-kwargs", '{"enable_thinking":true}',
                                              "--api-key", "must-not-record"]}}
                          for name in ("praxis-shared-gateway", "praxis-vllm")]
            def service_output(*command):
                if command[:2] == ("podman", "inspect"):
                    names = [container["Name"] for container in containers]
                    self.assertCountEqual(command[2:], names)
                    return json.dumps(containers)
                return '{"vllm":"0.19.0"}'
            with patch.object(integration, "service_output", side_effect=service_output):
                record = integration.runtime_metadata(inference, gateway)
            self.assertEqual(record["inference"], "cpu")
            self.assertEqual(record["settings"]["--revision"], "model-revision")
            self.assertEqual(record["packages"]["vllm"], "0.19.0")
            self.assertEqual(len(record["template_sha256"]), 64)
            self.assertNotIn("must-not-record", json.dumps(record))
            self.assertNotIn("private configuration", json.dumps(record))


class RunnerTest(unittest.TestCase):
    def test_model_selection_rejects_other_phases_before_ssh(self):
        args = ["run.py", "--host", "test@example.test", "--ssh-key", "/missing-key",
                "--scenario", "all-in-one", "--phase", "real-test", "--model", "qwen3.8-27b-int4"]
        error = io.StringIO()
        with patch.object(sys, "argv", args), patch.object(sys, "stderr", error), \
             patch.object(runner, "run") as execute, self.assertRaises(SystemExit):
            runner.main()
        self.assertIn("--model applies only to real-setup", error.getvalue())
        execute.assert_not_called()

    def test_deployment_bundle_includes_quota_commands_and_dependencies(self):
        data, manifest = runner.bundle()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for name in ("scripts/common/quota-status", "scripts/common/quota_status.py",
                         "scripts/common/quota-set", "scripts/common/quota_manage.py",
                         "scripts/common/quota_config.py", "scripts/common/provider_manage.py",
                         "scripts/common/provider_config.py", "scripts/common/lib.sh", "scripts/common/install"):
                self.assertIn(name, manifest)
                self.assertEqual(archive.extractfile(name).read(), (runner.ROOT / name).read_bytes())
            self.assertTrue(archive.getmember("scripts/common/quota-status").mode & 0o111)
            self.assertTrue(archive.getmember("scripts/common/quota-set").mode & 0o111)

    def test_bundle_excludes_generated_dependencies_but_rejects_source_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('configs','scripts','tests'):(root/name).mkdir()
            dependencies=root/'tests/browser/node_modules/.bin';dependencies.mkdir(parents=True)
            (dependencies/'playwright').symlink_to('/not-a-source-file')
            source=root/'scripts/run';source.write_text('reviewed source')
            with patch.object(runner,'ROOT',root):
                _,manifest=runner.bundle()
                self.assertEqual(set(manifest),{'scripts/run'})
                (root/'scripts/unsafe').symlink_to(source)
                with self.assertRaisesRegex(ValueError,'symlink'):runner.bundle()

    def test_candidate_override_rejects_other_phases_and_mutable_tags_before_ssh(self):
        image = "registry.example/vllm@sha256:" + "1" * 64
        for phase, candidate, message in (("real-test", image, "only to real-setup"),
                                          ("real-setup", "vllm:latest", "immutable"),
                                          ("real-setup", "", "immutable")):
            args = ["run.py", "--host", "test@example.test", "--ssh-key", "/missing-key",
                    "--scenario", "all-in-one", "--phase", phase, "--inference", "cpu",
                    "--vllm-image", candidate]
            error = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(sys, "stderr", error), \
                 patch.object(runner, "run") as execute, self.assertRaises(SystemExit):
                runner.main()
            self.assertIn(message, error.getvalue())
            execute.assert_not_called()

    def test_failed_command_streams_and_retains_output(self):
        terminal, log = io.StringIO(), io.StringIO()
        with redirect_stdout(terminal):
            code = runner.logged([sys.executable, "-c", "print('failure details'); raise SystemExit(7)"], log)
        self.assertEqual(code, 7)
        self.assertEqual(log.getvalue(), "failure details\n")
        self.assertEqual(terminal.getvalue(), log.getvalue())

    def test_bundle_excludes_workstation_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("configs", "scripts", "tests", ".state", ".git"):
                (root / name).mkdir()
                (root / name / "included.txt").write_text(name)
            for name in ("caller.secret", "credentials.local", "cache.pyc"):
                (root / "tests" / name).write_text("must stay local")
            with patch.object(runner, "ROOT", root):
                data, manifest = runner.bundle()
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                self.assertEqual(set(archive.getnames()), set(manifest))
            self.assertEqual(len(manifest), 3)
            self.assertTrue(all(len(value) == 64 for value in manifest.values()))

    def test_bundle_refuses_symlink_to_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            private = root / "private.key"
            private.write_text("private")
            (root / "configs/key").symlink_to(private)
            with patch.object(runner, "ROOT", root), self.assertRaises(ValueError):
                runner.bundle()


class RealAnswerTest(unittest.TestCase):
    def fixtures(self, text):
        usage = {"input_tokens": 10, "output_tokens": 20}
        chat = {"choices": [{"message": {"content": text, "reasoning_content": "ready"},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        responses = {"status": "completed", "usage": usage, "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "ready"}]},
            {"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        messages = {"stop_reason": "end_turn", "usage": usage,
                    "content": [{"type": "thinking", "thinking": "ready"}, {"type": "text", "text": text}]}
        streams = {
            "/v1/chat/completions": [
                {"choices": [{"delta": {"reasoning_content": "ready"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
                {"choices": [], "usage": chat["usage"]}, "[DONE]"],
            "/v1/responses": [
                {"type": "response.reasoning_summary_text.delta", "delta": "ready"},
                {"type": "response.output_text.delta", "delta": text},
                {"type": "response.completed", "response": responses}],
            "/v1/messages": [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "ready"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}},
                {"type": "message_stop"}]}
        for path, result in zip(streams, (chat, responses, messages)):
            yield path, json.dumps(result), False
            yield path, "".join("data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n"
                                for value in streams[path]), True

    def test_reasoning_alone_does_not_count_as_a_final_answer(self):
        for text in ("", "ready"):
            for path, body, stream in self.fixtures(text):
                with self.subTest(path=path, stream=stream, text=text):
                    if text:
                        integration.check_real_answer(path, body, stream)
                    else:
                        with self.assertRaises(AssertionError):
                            integration.check_real_answer(path, body, stream)

    def test_budget_exhaustion_and_missing_usage_are_failures(self):
        for path, body, stream in self.fixtures("ready"):
            with self.subTest(path=path, stream=stream):
                limited = body.replace('"stop"', '"length"').replace('"end_turn"', '"max_tokens"').replace('"completed"', '"incomplete"')
                with self.assertRaises(AssertionError):
                    integration.check_real_answer(path, limited, stream)
                with self.assertRaises(AssertionError):
                    integration.check_real_answer(path, body.replace('"usage"', '"absent"'), stream)


class HarnessTest(unittest.TestCase):
    def test_opencode_failed_tool_and_claude_nonmessage_events_are_not_passes(self):
        for code in (0, 1):
            log = json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {
                "status": "completed", "input": {"command": "python3 -m unittest -v"},
                "metadata": {"exit": code}}}})
            self.assertEqual(ran_tests("opencode", log), code == 0)
        self.assertFalse(ran_tests("claude", '{"type":"error","message":"unittest failed"}'))

    def test_timeout_stops_process_group_and_keeps_partial_output(self):
        with patch.object(integration.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaises(subprocess.TimeoutExpired) as error:
                integration.run_captured([sys.executable, "-u", "-c",
                    "import time; print('partial evidence'); time.sleep(60)"], timeout=0.3)
            self.assertIn("partial evidence", error.exception.output)
            self.assertTrue(any(call.args[1] == signal.SIGTERM for call in kill.call_args_list))

    def test_user_environment_is_not_put_in_process_arguments(self):
        with patch.object(integration.pwd, "getpwnam", return_value=Namespace(pw_dir="/home/test")), \
             patch.object(integration, "run_captured") as execute:
            integration.user_command("example", environment={"AUTH_TOKEN": "private-caller"})
        self.assertNotIn("private-caller", str(execute.call_args.args))
        self.assertTrue(Path(execute.call_args.args[0][0]).is_absolute())
        self.assertEqual(execute.call_args.kwargs["env"]["AUTH_TOKEN"], "private-caller")

    def test_model_claim_or_failed_command_is_not_a_successful_test_run(self):
        self.assertFalse(ran_tests("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"unittest passed"}}'))
        for code in (0, 1):
            log = json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                "command": "python3 -m unittest -v", "exit_code": code}})
            self.assertEqual(ran_tests("codex", log), code == 0)

    def test_real_tool_arguments_are_preserved_in_streams(self):
        for path, name in (("/v1/responses", "exec_command"),
                           ("/v1/chat/completions", "bash"), ("/v1/messages", "Bash")):
            body = {"tools": [{"name": name}]}
            with patch.object(harness.provider, "continuation", harness.continuation):
                response = harness.completion(path, body)
            stream = b"".join(harness.provider.events(path, response)).decode()
            self.assertIn(name, stream)
            self.assertIn("test_add.py", stream)
            self.assertIn("python3", stream)

    def test_prompt_or_failed_tool_cannot_complete_task(self):
        message = {"role": "user", "content": harness.MARKER}
        self.assertFalse(harness.continuation("/v1/messages", {"messages": [message]}))
        result = {"type": "tool_result", "tool_use_id": "call_fixture",
                  "content": harness.MARKER, "is_error": True}
        body = {"messages": [{"role": "user", "content": [result]}]}
        self.assertFalse(harness.continuation("/v1/messages", body))
        result["is_error"] = False
        self.assertTrue(harness.continuation("/v1/messages", body))

    def test_continuation_returns_final_text_instead_of_another_tool(self):
        body = {"tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_fixture", "content": harness.MARKER}]}]}
        with patch.object(harness.provider, "continuation", harness.continuation):
            response = harness.completion("/v1/messages", body)
        self.assertEqual(response["content"], [{"type": "text", "text": harness.MARKER}])


class ProfileTest(unittest.TestCase):
    def test_remote_transition_preserves_identity_with_private_installer_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            for name in ("tls.pem", "tls-key.pem", "jwt-public.pem"):
                (config / name).write_text(name)
                (config / name).chmod(0o640)
            def install(*command):
                for flag, name in (("--tls-cert", "tls.pem"), ("--tls-key", "tls-key.pem"),
                                   ("--jwt-public-key", "jwt-public.pem")):
                    path = Path(command[command.index(flag) + 1])
                    self.assertEqual(path.read_bytes(), (config / name).read_bytes())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual((config / name).stat().st_mode & 0o777, 0o640)
            with patch.object(host, "STATE", config), patch.object(host, "run", side_effect=install) as execute:
                host.restore_production(Namespace(scenario="remote-gateway", profile="memory"), {}, None, config)
                execute.assert_called_once()

    def test_mock_resume_recovers_unmounted_synthetic_secrets(self):
        args = Namespace(scenario="all-in-one", profile="memory")
        secrets = {"OPENAI_API_KEY": "praxis-openai-api-key-smoke-test",
                   "ANTHROPIC_API_KEY": "praxis-anthropic-api-key-smoke-test"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = dict(scenario=args.scenario, profile=args.profile, mock=True, secrets=secrets)
            (root / "state.json").write_text(json.dumps(state))
            with patch.object(host, "STATE", root), patch.object(host, "service"):
                self.assertEqual(host.mock_secrets(args, {}), secrets)
                state["mock"] = False
                (root / "state.json").write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, "mock-again"):
                    host.mock_secrets(args, {})

    def test_starting_a_new_mock_attempt_invalidates_both_pass_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("baseline-memory.json", "providers-memory.json"):
                (root / name).write_text("old pass")
            with patch.object(host, "STATE", root):
                host.invalidate_mock_results("memory")
            self.assertEqual(list(root.iterdir()), [])

    def test_switch_rejects_unowned_or_wrong_scenario_before_mutation(self):
        for state in ({"scenario": "remote-gateway", "profile": "memory", "mock": True},
                      {"scenario": "all-in-one", "profile": "memory", "mock": False}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "state.json").write_text(json.dumps(state))
                with patch.object(host, "STATE", root), patch.object(host, "run") as execute:
                    with self.assertRaises(ValueError):
                        host.switch_profile(Namespace(scenario="all-in-one", profile="valkey"))
                    execute.assert_not_called()

    def test_switch_verifies_before_uninstall_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = {"scenario": "all-in-one", "profile": "memory", "mock": True}
            (root / "state.json").write_text(json.dumps(old))
            calls = []
            with patch.object(host, "STATE", root), \
                 patch.object(host, "installed", side_effect=lambda args: calls.append("verify")), \
                 patch.object(host, "service", side_effect=lambda *args: calls.append("stop")), \
                 patch.object(host, "run", side_effect=lambda *args: calls.append(args)):
                host.switch_profile(Namespace(scenario="all-in-one", profile="valkey"))
            self.assertEqual(calls, ["verify", "stop", ("scripts/common/uninstall",)])
            self.assertFalse((root / "state.json").exists())
            self.assertEqual(json.loads(next(root.glob("state-memory-*.json")).read_text()), old)


if __name__ == "__main__":
    unittest.main()
