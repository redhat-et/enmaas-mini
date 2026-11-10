#!/usr/bin/env python3
"""Exercise pinned scenario images against private, observable provider fixtures."""

import base64
import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from evidence import redact, save
from provider import PATHS
from contracts import check

ROOT = Path(__file__).resolve().parents[2]
ENGINE = os.environ.get("CONTAINER_ENGINE", "podman")
IMAGE = os.environ.get("PRAXIS_IMAGE", "quay.io/opendatahub/praxis-experimental@sha256:"
                       "227d421e963c477038a884dc51ec880c5d0afa30098ae31028ecf85e963e40d5")
TOOL = ROOT / "scripts/remote-gateway/credentials"
VALKEY_IMAGE = "docker.io/valkey/valkey@sha256:63346cb24a61221e76bdf41acce99b3968a9fa83d8122144deab45394b27b4f2"
MOCK_IMAGE = "docker.io/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(redact(result.stderr or result.stdout))
    return result.stdout.strip()


def bind(source, target):
    """Read-only bind mount with a shared SELinux label.

    Rootless Podman on an SELinux-enforcing host (RHEL/Fedora) cannot read a
    user_home_t/user_tmp_t bind mount; relabeling to container_file_t matches the
    deployment, which labels /etc/praxis files via semanage. The ":z" option is a
    no-op on engines and hosts without SELinux.
    """
    mode = "ro,Z"
    return ["--volume", f"{source}:{target}:{mode}"]


def yaml_file(path):
    return json.loads(run("ruby", "-ryaml", "-rjson", "-e", "puts YAML.load_file(ARGV[0]).to_json", path))


def restart_gateway(name, port=8443, publisher=None):
    # Docker may assign a different random host port after a restart.
    run(ENGINE, "restart", name)
    return run(ENGINE, "port", publisher or name, f"{port}/tcp").rsplit(":", 1)[1]


def wait_for_gateway(request, attempts=30):
    last_result = "no response"
    for attempt in range(attempts):
        try:
            status = request("/v1/models", method="GET")
            if status == 401:
                return
            last_result = f"HTTP {status}"
        except (OSError, urllib.error.URLError) as error:
            if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
                raise
            last_result = str(error)
        if attempt + 1 < attempts:
            time.sleep(1)
    raise AssertionError(f"TLS/JWT endpoint did not become ready: {last_result}")


def expect_certificate_rejection(opener, url):
    try:
        with opener.open(url, timeout=5):
            pass
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            return
        raise AssertionError("expected a TLS certificate-verification failure, not HTTP or transport failure") from error
    raise AssertionError("invalid TLS certificate accepted")


def token(key, **overrides):
    claims = {"iss": "https://secure-single-server.local", "aud": "praxis-gateway", "sub": "test-user",
              "exp": int(time.time()) + 600}
    claims.update(overrides)
    enc = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=")
    message = enc(b'{"alg":"RS256","typ":"JWT"}') + b"." + enc(json.dumps(claims).encode())
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=message,
                               capture_output=True, check=True).stdout
    return (message + b"." + enc(signature)).decode()


class Gateway:
    """One test-owned network, mock namespace, gateway and optional Valkey."""
    def __init__(self, scenario, profile):
        self.scenario, self.profile = scenario, profile
        self.name = "praxis-mock-" + uuid.uuid4().hex[:12]
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.last_request = 0
        self.bearer = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def control(self, mode=None, **kwargs):
        req = urllib.request.Request(f"http://127.0.0.1:{self.control_port}" +
            ("/state" if mode is None else "/scenario"),
            data=None if mode is None else json.dumps({"mode": mode, **kwargs}).encode(),
            headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=5) as response:
            return json.load(response)

    def records(self):
        return self.control()["records"]

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException as error:
            self.diagnostics(error)
            self.close()
            raise

    def fixture_command(self):
        provider = self.work / "provider.py"
        shutil.copyfile(ROOT / "tests/common/provider.py", provider)
        return [*bind(provider, "/provider.py"),
                MOCK_IMAGE, "python3", "/provider.py", "--control-host", "0.0.0.0"]

    def start(self):
        for image in (IMAGE, MOCK_IMAGE, *([VALKEY_IMAGE] if self.profile == "valkey" else [])):
            available = subprocess.run([ENGINE, "image", "inspect", image], capture_output=True,
                                       timeout=30, check=False)
            if available.returncode:
                run(ENGINE, "pull", image)
            run("bash", "-c", 'source "$1"; check_native_image "$2" "$3"', "native",
                ROOT / "scripts/common/lib.sh", ENGINE, image)
        # Docker's internal bridge suppresses published ports. Providers bind
        # loopback inside this namespace; only gateway/control ports are mapped
        # to host loopback. No inference request uses an external destination.
        run(ENGINE, "network", "create", self.name)
        ports = [8443] if self.scenario == "remote" else [8080, 8081]
        mock_args = [ENGINE, "run", "--detach", "--name", self.name + "-mock", "--network", self.name,
                     "--read-only", "--cap-drop", "all", "--security-opt", "no-new-privileges",
                     "--user", "1001:1001", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        for port in [*ports, 19000]:
            mock_args += ["-p", f"127.0.0.1::{port}"]
        mock_args += self.fixture_command()
        run(*mock_args)
        self.control_port = self.port(19000)
        for _ in range(50):
            try:
                self.control()
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise AssertionError("mock control endpoint not ready")

        files = {}
        if self.scenario == "remote":
            run(TOOL, "init-jwt", "--directory", self.work / "issuer")
            run(TOOL, "lab-tls", "--directory", self.work / "tls", "--hostname", "localhost")
            rendered = self.work / "rendered.yaml"
            run("bash", "-c", 'source "$1"; render_remote_config "$2" "$3" "$4"', "render",
                ROOT / "scripts/common/lib.sh", self.profile, ROOT / "configs/remote-gateway/gateway.yaml", rendered)
            config = yaml_file(rendered)
            files = {"tls.pem": self.work / "tls/server.pem", "tls-key.pem": self.work / "tls/server-key.pem",
                     "jwt-public.pem": self.work / "issuer/public.pem",
                     "policy.yaml": ROOT / "configs/remote-gateway/policy.yaml"}
            self.key = self.work / "issuer/private.pem"
            self.bearer = token(self.key)
            context = ssl.create_default_context(cafile=str(self.work / "tls/ca.pem"))
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                      urllib.request.HTTPSHandler(context=context))
        else:
            suffix = "-valkey" if self.profile == "valkey" else ""
            config = yaml_file(ROOT / f"configs/all-in-one/shared-gateway{suffix}.yaml")
        # Test substitutions: destinations/TLS, synthetic secrets, quota sizes,
        # and owned paths only. Keep each scenario's complete production chain.
        for chain in config["filter_chains"]:
            for item in chain["filters"]:
                if item["filter"] == "token_rate_limit":
                    item["rules"][0].update(capacity=100, reserved_tokens=10)
                elif item["filter"] == "load_balancer":
                    for cluster in item["clusters"]:
                        cluster["endpoints"] = ["127.0.0.1:" + ("18080" if cluster["name"] == "openai" else "18081")]
                        cluster.pop("tls", None)
                        cluster.pop("http", None)
        config["insecure_options"] = {"allow_private_endpoints": True, "allow_private_upstreams": True}
        target = self.work / "gateway.json"
        target.write_text(json.dumps(config))
        files["shared-gateway.yaml"] = target
        self.config = config
        self.arguments = [ENGINE, "run", "--detach", "--name", self.name,
                     "--network", "container:" + self.name + "-mock", "--read-only", "--cap-drop", "all",
                     "--security-opt", "no-new-privileges", "--user", "1001:1001",
                     "-e", "OPENAI_API_KEY=synthetic-openai", "-e", "ANTHROPIC_API_KEY=synthetic-anthropic"]
        # keep-id is for rootless hosts. In a root-owned CI store it creates
        # a child user namespace that cannot mount sysfs for the fixture's
        # shared network namespace. --user still runs the process as non-root.
        if Path(ENGINE).name == "podman" and os.geteuid() != 0:
            self.arguments += ["--userns", "keep-id:uid=1001,gid=1001"]
        staging_root = self.work / "staged" / uuid.uuid4().hex
        for destination, source in files.items():
            if source.is_relative_to(self.work):
                # Subclasses may rewrite work-dir files in boot(); mount them live.
                mount_source = source
            else:
                # Stage repo files so the mount can be labeled without relabeling the checkout.
                mount_source = staging_root / destination
                mount_source.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, mount_source)
            mount_source.chmod(0o644)
            self.arguments += bind(mount_source, f"/etc/praxis/{destination}")
        if self.profile == "valkey":
            run(ENGINE, "volume", "create", self.name)
            acl = self.work / "users.acl"
            acl.write_text((ROOT / "configs/common/valkey/users.acl.example").read_text().replace(
                "SHA256_PASSWORD", "69d6dc9618d24d693cad07557702696090d0f575d9c0868384b8001fd1252358"))
            acl.chmod(0o644)
            vkconf = self.work / "valkey.conf"
            shutil.copyfile(ROOT / "configs/common/valkey/valkey.conf", vkconf)
            vkconf.chmod(0o644)
            vk_args = [ENGINE, "run", "--detach", "--name", self.name + "-valkey", "--network", self.name,
                       "--network-alias", "praxis-valkey", "--user", "999:999", "--read-only", "--cap-drop", "all",
                       "--security-opt", "no-new-privileges", "-v", self.name + ":/data",
                       *bind(acl, "/run/secrets/users.acl"),
                       *bind(vkconf, "/etc/valkey.conf")]
            if Path(ENGINE).name == "podman" and os.geteuid() != 0:
                vk_args += ["--userns", "keep-id:uid=999,gid=999"]
            run(*vk_args, VALKEY_IMAGE, "valkey-server", "/etc/valkey.conf")
            self.arguments += ["-e", "TOKEN_RATE_LIMIT_VALKEY_URL=redis://praxis:local-valkey-test@praxis-valkey:6379/0"]
        self.boot()

    def port(self, port):
        return run(ENGINE, "port", self.name + "-mock", f"{port}/tcp").rsplit(":", 1)[1]

    def boot(self):
        run(*self.arguments, IMAGE, "-c", "/etc/praxis/shared-gateway.yaml")
        self.openai_port = self.port(8443 if self.scenario == "remote" else 8080)
        self.anthropic_port = self.openai_port if self.scenario == "remote" else self.port(8081)
        self.ready()

    def ready(self):
        if self.scenario == "remote":
            wait_for_gateway(lambda path, method: self.request(path, method=method, bearer="")[0])
            return
        metrics_address = self.config.get('admin', {}).get(
            'metrics_address', '127.0.0.1:9901')
        metrics_port = metrics_address.rsplit(':', 1)[1]
        for _ in range(40):
            try:
                # GET /v1/models traverses this profile's unconditional
                # quota filter and consumes a reservation without usage.
                run(ENGINE, "exec", self.name + "-mock", "python3", "-c",
                    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:' + metrics_port +
                    '/healthy", timeout=1).read()')
                return
            except (OSError, urllib.error.URLError, RuntimeError):
                pass
            time.sleep(0.25)
        raise AssertionError("gateway did not become ready")

    def reset(self, capacity=100, first_match=False):
        run(ENGINE, "rm", "--force", self.name)
        for chain in self.config["filter_chains"]:
            for item in chain["filters"]:
                if item["filter"] == "token_rate_limit":
                    item["rules"] = item["rules"][:1]
                    item["rules"][0]["capacity"] = capacity
                    # Isolate persistent counters between cases, keeping their
                    # production backend/ACL and restart behavior unchanged.
                    if self.profile == "valkey":
                        item["backend"]["namespace"] = "secure-single-server:limits:fixture:" + uuid.uuid4().hex
                    if first_match:
                        second = copy.deepcopy(item["rules"][0])
                        second.update(name="unselected-catch-all", capacity=10)
                        item["rules"].append(second)
        (self.work / "gateway.json").write_text(json.dumps(self.config))
        self.control("ok", reset=True)
        self.boot()

    def request(self, path, body=None, method="POST", bearer=None, timeout=5):
        # Preserve shipped request throttling; quota checks must not confuse it
        # with token admission. Pace all requests across the shared remote chain.
        time.sleep(max(0, 0.55 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {"Content-Type": "application/json", "X-Model": "spoofed-model",
                   "X-Api-Key": "spoofed-provider-key", "X-Tier": "spoofed-tier",
                   "X-Cluster": "spoofed-cluster", "X-Selected-Model": "spoofed-model", "X-Route": "spoofed-route"}
        selected = self.bearer if bearer is None else bearer
        if selected:
            headers["Authorization"] = "Bearer " + selected
        if body is None:
            body = {"model": "fixture", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4}
            if path == "/v1/responses":
                body = {"model": "fixture", "input": "hello", "max_output_tokens": 4}
        port = self.anthropic_port if path.startswith("/v1/messages") else self.openai_port
        scheme = "https" if self.scenario == "remote" else "http"
        req = urllib.request.Request(f"{scheme}://localhost:{port}{path}",
            data=json.dumps(body).encode() if method == "POST" else None, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read().decode(), response.headers

    def close(self):
        for args in (["rm", "--force", self.name], ["rm", "--force", self.name + "-mock"],
                     ["rm", "--force", self.name + "-valkey"], ["network", "rm", self.name],
                     ["volume", "rm", self.name]):
            subprocess.run([ENGINE, *args], capture_output=True, timeout=30, check=False)
        self.temp.cleanup()

    def __exit__(self, kind, value, traceback):
        if kind:
            self.diagnostics(value)
        self.close()

    def diagnostics(self, error):
        logs = {}
        for suffix in ("", "-mock", "-valkey"):
            try:
                result = subprocess.run([ENGINE, "logs", "--tail", "40", self.name + suffix],
                                        capture_output=True, text=True, timeout=15, check=False)
                logs[suffix or "gateway"] = redact(result.stdout + result.stderr)
            except (OSError, subprocess.TimeoutExpired):
                logs[suffix or "gateway"] = "diagnostic log unavailable"
        save(f"{ENGINE}-{self.scenario}-{self.profile}-failure", {
            "engine": ENGINE, "status": "failed", "error": str(error), "logs": logs})


def request_body(path, stream=False, tools=False, result=False):
    body = {"model": "fixture", "stream": stream}
    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
              "required": ["a", "b"]}
    function = {"name": "add", "description": "Add two integers", "parameters": schema}
    if path == "/v1/responses":
        body["input"] = [{"type": "function_call_output", "call_id": "call_fixture", "output": "5"}] if result else "hello"
        if result:
            body["previous_response_id"] = "resp_fixture"
        if tools:
            body["tools"] = [{"type": "function", **function}]
    else:
        body.update(messages=[{"role": "user", "content": "hello"}], max_tokens=4)
        if path == "/v1/messages":
            if tools:
                body["tools"] = [{"name": "add", "description": "Add two integers", "input_schema": schema}]
            if result:
                body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture",
                    "name": "add", "input": {"a": 2, "b": 3}}]}, {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_fixture", "content": "5"}]}]
        else:
            if stream:
                body["stream_options"] = {"include_usage": True}
            if tools:
                body["tools"] = [{"type": "function", "function": function}]
            if result:
                body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_fixture", "type": "function", "function": {"name": "add", "arguments": '{"a":2,"b":3}'}}]},
                    {"role": "tool", "tool_call_id": "call_fixture", "content": "5"}]
    return body


def expect(gateway, path, status=200, forwarded=True, **kwargs):
    before = len(gateway.records())
    actual, body, headers = gateway.request(path, **kwargs)
    assert actual == status, f"{path}: expected HTTP {status}, got {actual}: {body[:200]}"
    records = gateway.records()
    assert len(records) == before + int(forwarded), f"{path}: incorrect upstream request count"
    if forwarded:
        record = records[-1]
        assert record["path"] == path and record["model"] == "fixture", record
        assert record["credential_ok"] and record["classification_clean"], record
    return body, headers


def contracts(gateway):
    for path in PATHS:
        for stream in (False, True):
            gateway.reset()
            body, headers = expect(gateway, path, body=request_body(path, stream))
            check(path, body, stream)
            if stream:
                assert "text/event-stream" in headers["Content-Type"]
                terminal = "[DONE]" if path == PATHS[0] else ("response.completed" if path == PATHS[1] else "message_stop")
                assert terminal in body, "stream did not complete"
            else:
                assert json.loads(body)["model"] == "fixture"
            body, _ = expect(gateway, path, body=request_body(path, stream, tools=True))
            check(path, body, stream, tool=True)
            body, _ = expect(gateway, path, body=request_body(path, stream, tools=True, result=True))
            check(path, body, stream, continued=True)
            assert gateway.records()[-1]["continuation"], "tool result lost"


def quotas(gateway):
    for path in PATHS:
        for stream in (False, True):
            gateway.reset(capacity=20, first_match=True)
            # Five reported tokens settle a reservation of ten: exactly three
            # requests fit. No settlement admits two; no accounting admits four.
            for _ in range(3):
                expect(gateway, path, body=request_body(path, stream))
            expect(gateway, path, status=429, forwarded=False)
            if gateway.scenario == "remote":
                expect(gateway, path, status=429, forwarded=False,
                       bearer=token(gateway.key, sub="another-user"))
            alternate = "/v1/chat/completions" if path == "/v1/responses" else "/v1/responses"
            if path != "/v1/messages":
                expect(gateway, alternate, status=429, forwarded=False)
            independent = "/v1/responses" if path == "/v1/messages" else "/v1/messages"
            expect(gateway, independent)
    # Retain restart/persistence/outage assertions using both live providers.
    if gateway.scenario == "remote":
        expect(gateway, "/v1/messages/batches", status=404, forwarded=False)
    if gateway.profile == "valkey":
        time.sleep(2)
        run(ENGINE, "restart", gateway.name + "-valkey")
    gateway.openai_port = restart_gateway(gateway.name,
        port=8443 if gateway.scenario == "remote" else 8080, publisher=gateway.name + "-mock")
    gateway.anthropic_port = gateway.openai_port if gateway.scenario == "remote" else gateway.port(8081)
    gateway.ready()
    if gateway.profile == "memory":
        expect(gateway, "/v1/messages")
    else:
        expect(gateway, "/v1/messages", status=429, forwarded=False)
        run(ENGINE, "stop", "--time", "1", gateway.name + "-valkey")
        expect(gateway, "/v1/responses", status=503, forwarded=False)
        if gateway.scenario == "remote":
            expect(gateway, "/v1/messages/batches", status=404, forwarded=False)
        run(ENGINE, "start", gateway.name + "-valkey")
        for _ in range(30):
            try:
                if run(ENGINE, "exec", "-e", "VALKEYCLI_AUTH=local-valkey-test", gateway.name + "-valkey",
                       "valkey-cli", "--user", "praxis", "PING") == "PONG":
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("Valkey did not recover")
        # The remaining OpenAI allowance survives the outage; recovery must
        # neither reset counters nor leave admission permanently closed.
        expect(gateway, "/v1/responses")
        expect(gateway, "/v1/responses")
        expect(gateway, "/v1/responses", status=429, forwarded=False)


def failures(gateway):
    for path in PATHS:
        for stream in (False, True):
            gateway.reset(capacity=20)
            gateway.control("missing_usage")
            for _ in range(2):
                body, _ = expect(gateway, path, body=request_body(path, stream))
                assert '"usage"' not in body
            expect(gateway, path, status=429, forwarded=False)
        for mode, status in (("http429", 429), ("http500", 500)):
            gateway.reset(capacity=20)
            gateway.control(mode)
            for _ in range(2):
                body, _ = expect(gateway, path, status=status)
                assert mode in body, "provider error not preserved"
            expect(gateway, path, status=429, forwarded=False)
        gateway.reset()
        gateway.control("delayed", delay=0.05)
        body, _ = expect(gateway, path, body=request_body(path, stream=True))
        assert "usage" in body, "delayed stream lost final usage"
        gateway.reset(capacity=20)
        gateway.control("cancelled", delay=0.05)
        # Anthropic reports two input tokens in message_start, so a truncated
        # stream settles that partial usage. OpenAI has no usage until its
        # final event and retains the reservation. Assert the shipped behavior.
        for _ in range(6 if path == "/v1/messages" else 2):
            body, _ = expect(gateway, path, body=request_body(path, stream=True))
            assert "[DONE]" not in body and "response.completed" not in body and "message_stop" not in body
        expect(gateway, path, status=429, forwarded=False)
        gateway.reset(capacity=20)
        gateway.control("timeout", delay=10)
        for _ in range(2):
            before = len(gateway.records())
            try:
                # Include TLS and quota admission on loaded CI runners. The
                # provider waits 10s, so this still proves client cancellation
                # before its response, without a 200ms scheduling assumption.
                gateway.request(path, timeout=3)
            except (TimeoutError, urllib.error.URLError) as error:
                assert isinstance(getattr(error, "reason", error), TimeoutError), "unrelated transport failure"
            else:
                raise AssertionError("delayed provider did not time out")
            assert len(gateway.records()) == before + 1, "timed out request never reached provider"
        expect(gateway, path, status=429, forwarded=False)


def security(gateway):
    if gateway.scenario != "remote":
        return
    gateway.reset()
    for bad in ["", "invalid", token(gateway.key, exp=int(time.time()) - 600),
                token(gateway.key, iss="https://wrong.local"), token(gateway.key, aud="wrong"),
                gateway.bearer[:-10] + "invalidsig"]:
        expect(gateway, "/v1/messages", bearer=bad, status=401, forwarded=False)
    expect(gateway, "/not-an-api", status=404, forwarded=False)
    for path, method in [("/v1/messages/batches", "POST"), ("/v1/messages/batches", "GET"),
                         ("/v1/messages/batches/batch-test/results", "GET"),
                         ("/v1/messages/batches/batch-test/cancel", "POST")]:
        expect(gateway, path, method=method, status=404, forwarded=False)
    expect(gateway, "/v1/messages/count_tokens")
    untrusted = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    expect_certificate_rejection(untrusted, f"https://localhost:{gateway.openai_port}/v1/models")
    expect_certificate_rejection(gateway.opener, f"https://127.0.0.1:{gateway.openai_port}/v1/models")
    try:
        with untrusted.open(f"http://localhost:{gateway.openai_port}/v1/models", timeout=5) as response:
            assert response.status >= 400, "plaintext inference accepted"
    except (urllib.error.URLError, http.client.HTTPException, ConnectionResetError):
        pass


def main(argv=None):
    if not __debug__ or os.environ.get("PYTHONOPTIMIZE"):
        raise SystemExit("Run tests without Python -O/PYTHONOPTIMIZE")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all-in-one", "remote"), default="all-in-one")
    parser.add_argument("--valkey", action="store_true")
    parser.add_argument("--case", choices=("all", "contracts", "quotas", "failures", "security"), default="all")
    args = parser.parse_args(argv)
    profile = "valkey" if args.valkey else "memory"
    result = {"engine": ENGINE, "scenario": args.scenario, "profile": profile, "status": "failed", "cases": []}
    try:
        with Gateway(args.scenario, profile) as gateway:
            for test in (security, contracts, quotas, failures):
                if test == security and args.scenario != "remote":
                    continue
                if args.case in ("all", test.__name__):
                    test(gateway)
                    result["cases"].append(test.__name__)
                    print(f"PASS {args.scenario}/{profile}: {test.__name__}", flush=True)
        result["status"] = "passed"
    finally:
        save(f"{ENGINE}-{args.scenario}-{profile}", result)


if __name__ == "__main__":
    main()
