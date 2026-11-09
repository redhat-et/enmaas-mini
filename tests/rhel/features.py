#!/usr/bin/env python3
"""Reversible feature qualification on a test-owned, mock-only RHEL gateway."""
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import sys
import time
import uuid

from host import ROOT, STATE, installed, run
sys.path.insert(0, str(ROOT / "scripts/common"))
from provider_manage import atomic_write, transaction, restart


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require_mock(state, config, scenario, profile):
    if not state.get("mock") or state.get("scenario") != scenario or state.get("profile") != profile:
        raise ValueError("feature tests require this suite's matching mock installation; real services are refused")
    clusters = [c for chain in config["filter_chains"] for f in chain["filters"]
                if f["filter"] == "load_balancer" for c in f["clusters"]]
    allowed = {"praxis-rhel-mock:18080", "praxis-rhel-mock:18081", "praxis-vllm:8000"}
    if not clusters or any(not c.get("endpoints") or not set(c["endpoints"]) <= allowed for c in clusters):
        raise ValueError("feature tests refuse nonfixture upstreams")


def quota_config(original, namespace):
    config = copy.deepcopy(original)
    for index, item in enumerate(f for chain in config["filter_chains"] for f in chain["filters"]):
        if item["filter"] != "token_rate_limit":
            continue
        for rule in item["rules"]:
            rule.update(capacity=20, reserved_tokens=10, window="60s", reservation_timeout="30s")
        if item["backend"]["kind"] == "valkey":
            item["backend"]["namespace"] = f"secure-single-server:limits:qualification:{namespace}:{index}"
    return config


def restore_limits(config, manifest, recovery, gid, *, activate=restart):
    saved = json.loads(recovery.read_text())
    if str(config) != saved["config"] or str(manifest) != saved["manifest"]:
        raise ValueError("recovery belongs to different managed paths")
    if digest(config.read_bytes()) not in saved["expected_config_hashes"]:
        raise ValueError("configuration changed during feature tests; inspect the private recovery file")
    current = dict(line.split("  ", 1)[::-1] for line in manifest.read_text().splitlines())
    if current != saved["manifest_entries"] and current != {
            **saved["manifest_entries"], str(config): digest(config.read_bytes())}:
        raise ValueError("manifest changed during feature tests; inspect the private recovery file")
    for path, expected in current.items():
        if path != str(config) and digest(Path(path).read_bytes()) != expected:
            raise ValueError("protected managed file changed during feature tests")
    transaction({config: base64.b64decode(saved["original"])}, manifest, gid, activate=activate)
    recovery.unlink()


@contextmanager
def temporary_limits(config, manifest, recovery, gid, *, activate=restart):
    if recovery.exists():
        raise ValueError("unfinished feature test: run features-restore first")
    original = config.read_bytes()
    import yaml
    source = yaml.safe_load(original)
    saved = {"config": str(config), "manifest": str(manifest), "original": base64.b64encode(original).decode(),
             "manifest_entries": dict(line.split("  ", 1)[::-1] for line in manifest.read_text().splitlines()),
             "expected_config_hashes": [digest(original)]}
    atomic_write(recovery, json.dumps(saved).encode(), os.geteuid(), gid, 0o600)

    def reset():
        data = (json.dumps(quota_config(source, uuid.uuid4().hex), indent=2) + "\n").encode()
        # Persist the next hash before activation so interruption remains recoverable.
        saved["expected_config_hashes"].append(digest(data))
        atomic_write(recovery, json.dumps(saved).encode(), os.geteuid(), gid, 0o600)
        transaction({config: data}, manifest, gid, activate=activate)

    try:
        yield reset
    finally:
        restore_limits(config, manifest, recovery, gid, activate=activate)


def body(gateway, path, stream=False):
    request = {"model": gateway.model, "input": "hello", "messages": [], "stream": stream, "max_tokens": 4}
    # These legacy quota profiles do not inject stream_options. Ask for usage
    # explicitly so this test measures settlement, not reservation fallback.
    if stream and path == '/v1/chat/completions':
        request['stream_options'] = {'include_usage': True}
    return request


def exhaust(gateway, path, stream=False):
    for _ in range(3):
        status, _ = gateway.request(path, body(gateway, path, stream))
        assert status == 200, f"{path}: expected successful settlement, got {status}"
    before = len(gateway.control()["records"])
    status, _ = gateway.request(path, body(gateway, path, stream))
    assert status == 429, f"{path}: fourth reservation should be denied, got {status}"
    assert len(gateway.control()["records"]) == before, "denied request reached provider"


def checks(gateway, reset, results, *, sleep=time.sleep):
    for path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
        for stream in (False, True):
            reset()
            gateway.control("ok", reset=True)
            exhaust(gateway, path, stream)
            results[f"Q1:{path}:stream={stream}"] = "passed"
    reset()
    gateway.control("ok", reset=True)
    for path in ("/v1/chat/completions", "/v1/responses", "/v1/chat/completions"):
        assert gateway.request(path, body(gateway, path))[0] == 200
    before = len(gateway.control()["records"])
    assert gateway.request("/v1/responses", body(gateway, "/v1/responses"))[0] == 429
    assert len(gateway.control()["records"]) == before
    assert gateway.request("/v1/messages", body(gateway, "/v1/messages"))[0] == 200
    results["Q3:shared-Chat-Responses-independent-Messages"] = "passed"
    # No restart/reset between denial and recovery.
    started = time.monotonic()
    sleep(61)
    assert gateway.request("/v1/responses", body(gateway, "/v1/responses"))[0] == 200
    results["Q7:recovery-without-restart"] = {"status": "passed", "wait_seconds": round(time.monotonic() - started, 2)}
    reset()
    gateway.control("ok", reset=True)
    for _ in range(3):
        assert gateway.request("/v1/models?limit=1000", method="GET")[0] == 200
        assert gateway.request("/v1/messages/count_tokens", body(gateway, "/v1/messages"))[0] == 200
    exhaust(gateway, "/v1/responses")
    exhaust(gateway, "/v1/messages")
    results["Q9:catalog-and-token-count-do-not-spend-inference-allowance"] = "passed"
    if gateway.args.scenario == "remote-gateway":
        reset()
        gateway.control("ok", reset=True)
        for token in ("", "invalid"):
            assert gateway.request("/v1/models", method="GET", token=token)[0] == 401
            assert gateway.request("/v1/responses", token=token)[0] == 401
        assert not gateway.control()["records"]
        exhaust(gateway, "/v1/responses")
        results["Q10:missing-invalid-auth-no-forward-or-charge"] = "passed"


def main(args):
    from integration import InstalledGateway, setup_harnesses, user_command, configuration, USER
    import subprocess
    import yaml
    config = Path("/etc/praxis/shared-gateway.yaml")
    manifest = config.with_suffix(".manifest")
    recovery = STATE / "features-restore.json"
    gid = pwd.getpwnam("praxis-svc").pw_gid

    def activate():
        run("restorecon", "-F", config)
        restart()

    if args.phase == "features-restore":
        restore_limits(config, manifest, recovery, gid, activate=activate)
        run("scripts/common/verify", "--host")
        return
    state = json.loads((STATE / "state.json").read_text())
    require_mock(state, yaml.safe_load(config.read_text()), args.scenario, args.profile)
    installed(args)  # Managed manifest and synthetic secret references.
    from host import service_output
    assert service_output("podman", "inspect", "praxis-rhel-mock", "--format",
                          '{{index .Config.Labels "rhel-smoke"}}') == "true"
    # The vLLM alias must belong to the mock; real vLLM must not be running.
    assert not service_output("podman", "ps", "--filter", "name=^praxis-vllm$", "--format", "{{.Names}}")
    setup_harnesses()
    args.provider = args.feature_provider
    gateway = InstalledGateway(args)
    tag = f"features-{args.scenario}-{args.profile}-{args.provider}-{time.time_ns()}"
    result = {"scenario": args.scenario, "profile": args.profile, "provider": args.provider,
              "config_sha256": digest(config.read_bytes()), "checks": {}, "harness_denial": {},
              "not_run": ["Q2:between-tools-and-native-recovery", "Q3:two-callers", "Q4", "Q5", "Q6", "Q8",
                          "Q10:expired-JWT-and-upstream-errors", "interactive-selection", "compaction", "restart-resume"],
              "status": "running"}
    def interrupted(*_):
        raise InterruptedError("feature test interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with temporary_limits(config, manifest, recovery, gid, activate=activate) as reset:
            checks(gateway, reset, result["checks"])
            account = pwd.getpwnam(USER)
            for name in ("codex", "opencode", "claude"):
                if args.harness and name != args.harness:
                    continue
                reset()
                gateway.control("ok", reset=True)
                path = "/v1/messages" if name == "claude" else "/v1/responses"
                exhaust(gateway, path)
                before = len(gateway.control()["records"])
                directory = Path(account.pw_dir) / (tag + "-" + name)
                directory.mkdir(mode=0o700)
                os.chown(directory, account.pw_uid, account.pw_gid)
                assert user_command("git", "init", "-q", directory).returncode == 0
                provider = gateway.provider if gateway.provider != "cloud" else ("anthropic" if name == "claude" else "openai")
                base = gateway.base.removesuffix(gateway.prefix) if gateway.prefix else gateway.base
                command, env = configuration(name, provider, gateway.model, base, gateway.token,
                    prompt="Reply with ready. Do not create or change files.",
                    messages_base=base if args.scenario == "remote-gateway" else "http://127.0.0.1:8081")
                if args.scenario == "remote-gateway":
                    ca = directory / "ca.pem"
                    ca.write_bytes((ROOT / "material/ca.pem").read_bytes())
                    os.chown(ca, account.pw_uid, account.pw_gid)
                    env.update(SSL_CERT_FILE=str(ca), NODE_EXTRA_CA_CERTS=str(ca))
                started = time.monotonic()
                try:
                    process = user_command(*command, directory=directory, environment=env, timeout=20)
                    code, output = process.returncode, process.stdout + process.stderr
                except subprocess.TimeoutExpired as error:
                    code, output = 124, (error.output or "") + (error.stderr or "")
                (STATE / f"{tag}-{name}.log").write_text(output.replace(gateway.token, "[caller]"))
                assert len(gateway.control()["records"]) == before, f"{name} forwarded inference during exhaustion"
                assert code != 0, f"{name} reported success despite exhausted quota"
                assert re.search(r"\b429\b|rate.?limit|quota.?exceed", output, re.I), \
                    f"{name} did not report a quota denial; unrelated startup failures do not qualify"
                result["harness_denial"][name] = {"status": "passed-initial-denial", "exit_code": code,
                    "seconds": round(time.monotonic() - started, 2), "cancelled_at_deadline": code == 124}
        run("scripts/common/verify", "--host")
        assert digest(config.read_bytes()) == result["config_sha256"]
        result["status"] = "passed-automated-subset"
    finally:
        signal.signal(signal.SIGTERM, previous)
        if result["status"] == "running":
            result["status"] = "failed"
        result["restored"] = not recovery.exists() and digest(config.read_bytes()) == result["config_sha256"]
        (STATE / (tag + ".json")).write_text(json.dumps(result, indent=2) + "\n")
        print("Feature evidence: " + str(STATE / (tag + ".json")), flush=True)
