#!/usr/bin/env python3
"""Cloud-free tests of the AWS resource plan and mutation guard."""
import importlib.machinery
import importlib.util
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader("rhel_vm", str(ROOT / "scripts/aws/rhel-vm"))
spec = importlib.util.spec_from_loader(loader.name, loader)
vm = importlib.util.module_from_spec(spec)
loader.exec_module(vm)


class PlanTest(unittest.TestCase):
    def test_group_tags_are_derived_from_run_and_legacy_tags_are_unchanged(self):
        prefix = 'em-demo-0123456789ab'
        for role, scenario in [('gateway', 'remote-gateway'), ('keycloak', 'keycloak'), ('observability', 'observability')]:
            tags = {row['Key']: row['Value'] for row in vm.tags(prefix+'-'+role, scenario)}
            self.assertEqual(tags['enmaas:group'], 'demo')
            self.assertEqual(tags['enmaas:run'], prefix)
            self.assertEqual(tags['enmaas:purpose'], 'external-services-test')
        with self.assertRaises(ValueError):
            vm.tags(prefix+'-gateway', 'keycloak')
        for legacy in ('enmaas-0123456789abcdef-gateway', 'pricetag-test-real'):
            self.assertNotIn('enmaas:group', {row['Key']: row['Value'] for row in vm.tags(legacy, 'remote-gateway')})

    def test_capacity_report_lists_only_public_subnets_without_claiming_free_instances(self):
        aws = vm.Aws("eu-central-1", None, False)
        args = SimpleNamespace(account_id="123456789012", instance_type="g6.2xlarge", subnet_id="subnet-a")
        def subnet(name, zone, count=10):
            return {"SubnetId": name, "VpcId": "vpc-test", "AvailabilityZone": zone,
                    "AvailableIpAddressCount": count, "State": "available", "OwnerId": args.account_id}
        route = {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-test", "State": "active"}
        replies = [
            {"Account": args.account_id, "Arn": "arn:aws:iam::123456789012:user/test"},
            {"Subnets": [subnet("subnet-a", "eu-central-1a")]},
            {"InstanceTypeOfferings": [{"Location": "eu-central-1a"}, {"Location": "eu-central-1b"}]},
            {"Subnets": [subnet("subnet-a", "eu-central-1a"), subnet("subnet-b", "eu-central-1b"),
                         subnet("subnet-private", "eu-central-1b"), subnet("subnet-full", "eu-central-1b", 0),
                         subnet("subnet-unsupported", "eu-central-1c")]},
            {"RouteTables": [{"Associations": [{"Main": True}], "Routes": [route]},
                             {"Associations": [{"SubnetId": "subnet-private"}], "Routes": []}]}]
        with patch.object(aws, "call", side_effect=replies) as calls:
            report = vm.capacity_report(aws, args)
        self.assertEqual(report["SpareInstanceCapacity"], "unknown")
        self.assertEqual([s["SubnetId"] for s in report["PublicSubnetCandidates"]], ["subnet-a", "subnet-b"])
        self.assertEqual(report["SelectedAvailabilityZone"], "eu-central-1a")
        self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

    def test_capacity_command_needs_no_journal_or_launch_settings(self):
        argv = ["rhel-vm", "capacity", "--region", "eu-central-1", "--account-id", "123456789012",
                "--instance-type", "g6.2xlarge", "--subnet-id", "subnet-a"]
        with patch.object(vm.sys, "argv", argv), patch.object(vm, "capacity_report", return_value={}) as report, \
                patch.object(vm, "apply_plan") as apply, contextlib.redirect_stdout(io.StringIO()):
            vm.main()
        report.assert_called_once()
        self.assertFalse(report.call_args.args[0].apply)
        apply.assert_not_called()

    def test_capacity_error_is_not_reported_as_bad_credentials(self):
        aws = vm.Aws("eu-central-1", None, True)
        error = SimpleNamespace(returncode=1, stderr="An error occurred (InsufficientInstanceCapacity): details", stdout="")
        with patch.object(vm.subprocess, "run", return_value=error):
            with self.assertRaisesRegex(ValueError, "capacity.*Availability Zone") as caught:
                aws.call("ec2", "run-instances")
        self.assertNotIn("credentials", str(caught.exception))

    def test_complete_plans_cover_each_scenario_and_hardware_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "test.pub"
            key.write_text("ssh-ed25519 synthetic-test-key\n")
            for scenario in ["all-in-one", "remote-gateway"]:
                for arch, native, inference, instance, disk in [
                        ("amd64", "x86_64", "none", "m7i.2xlarge", 50),
                        ("arm64", "arm64", "none", "m7g.2xlarge", 50),
                        ("amd64", "x86_64", "cpu", "m7i.4xlarge", 100),
                        ("amd64", "x86_64", "gpu", "g6.2xlarge", 200)]:
                    args = self.settings(account_id="123456789012", arch=arch, inference=inference,
                        subnet_id="subnet-test", prefix="test-" + scenario, public_key=key,
                        scenario=scenario, allowed_cidr="192.0.2.1/32", region="eu-central-1",
                        state_file=Path(directory) / (scenario + arch + inference + ".json"))
                    vm.configure(args)
                    machine = {"MemoryInfo": {"SizeInMiB": 65536 if inference == "cpu" else 32768},
                               "ProcessorInfo": {"SupportedArchitectures": [native]}}
                    if inference == "gpu":
                        machine["GpuInfo"] = {"Gpus": [{"Name": "L4", "Manufacturer": "NVIDIA", "Count": 1,
                                                      "MemoryInfo": {"SizeInMiB": 23040}}]}
                    aws = vm.Aws(args.region, None, False)
                    responses = [
                        {"Account": args.account_id, "Arn": "arn:aws:iam::123456789012:user/test"},
                        {"InstanceTypes": [machine]},
                        {"Images": [{"ImageId": "ami-test", "CreationDate": "2026-09-01", "OwnerId": vm.OWNER,
                            "Architecture": native, "State": "available", "RootDeviceType": "ebs",
                            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {}}],
                            "RootDeviceName": "/dev/sda1", "Name": f"RHEL-9.6_HVM-test-{native}-0-Hourly2-GP3"}]},
                        {"Subnets": [{"State": "available", "VpcId": "vpc-test", "AvailabilityZone": "eu-central-1a"}]},
                        {"InstanceTypeOfferings": [{"Location": "eu-central-1a"}]},
                        {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0",
                                                      "GatewayId": "igw-test", "State": "active"}]}]},
                        {"Reservations": []}, {"SecurityGroups": []}, {"KeyPairs": []}]
                    with patch.object(aws, "call", side_effect=responses) as calls, \
                            patch.object(vm.subprocess, "run"):
                        proposal = vm.plan(aws, args)
                    self.assertEqual(proposal["Architecture"], native)
                    self.assertEqual((proposal["Inference"], proposal["InstanceType"], proposal["VolumeGiB"]),
                                     (inference, instance, disk))
                    self.assertEqual(proposal["AvailabilityZone"], "eu-central-1a")
                    self.assertEqual(len(proposal["Ingress"]), 2 if scenario == "remote-gateway" else 1)
                    self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))
                    self.assertFalse(args.state_file.exists())

    def settings(self, **overrides):
        return SimpleNamespace(**dict.fromkeys(vm.CONFIG_KEYS | {"config"}) | overrides)

    def test_config_defaults_and_explicit_overrides(self):
        for name, kind, machine, disk in (("no-vllm", "none", "m7i.2xlarge", 50),
                ("vllm-cpu", "cpu", "m7i.4xlarge", 100), ("vllm-gpu", "gpu", "g6.2xlarge", 200)):
            args = self.settings(config=ROOT / f"configs/aws/{name}.json", scenario="all-in-one")
            vm.configure(args)
            self.assertEqual((args.inference, args.instance_type, args.volume_gib), (kind, machine, disk))
        args = self.settings(config=ROOT / "configs/aws/vllm-gpu.json", scenario="remote-gateway",
                             instance_type="g6.4xlarge", volume_gib=300)
        vm.configure(args)
        self.assertEqual((args.scenario, args.inference, args.instance_type, args.volume_gib),
                         ("remote-gateway", "gpu", "g6.4xlarge", 300))
        args = self.settings(scenario="all-in-one", inference="cpu")
        vm.configure(args)
        self.assertEqual((args.instance_type, args.volume_gib), ("m7i.4xlarge", 100))
        args = self.settings(scenario="all-in-one", arch="arm64")
        vm.configure(args)
        self.assertEqual(args.instance_type, "m7g.2xlarge")

    def test_vllm_server_scenario_requires_inference_hardware(self):
        for inference, instance, disk in (("cpu", "m7i.4xlarge", 100),
                                          ("gpu", "g6.2xlarge", 200)):
            args = self.settings(scenario="vllm-server", inference=inference)
            vm.configure(args)
            self.assertEqual((args.instance_type, args.volume_gib), (instance, disk))
        with self.assertRaisesRegex(ValueError, "vllm-server requires"):
            vm.configure(self.settings(scenario="vllm-server", inference="none"))

    def test_invalid_config_is_rejected_before_cloud_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "vm.json"
            for invalid in (["gpu"], {"access_key": "never-accept"}, {"volume_gib": True},
                    {"volume_gib": "200"}, {"scenario": "typo"}, {"inference": "cuda"},
                    {"arch": "x86_64"}, {"arch": "arm64", "inference": "cpu"},
                    {"inference": "gpu", "volume_gib": 100}, {"inference": "cpu", "volume_gib": 50},
                    {"volume_gib": 16385}, {"instance_type": ""}, {"ami_id": "bad"}):
                config.write_text(json.dumps({"scenario": "all-in-one", **invalid}
                                             if isinstance(invalid, dict) else invalid))
                with self.subTest(invalid=invalid), patch.object(vm.Aws, "call") as calls:
                    with self.assertRaises(ValueError):
                        vm.configure(self.settings(config=config))
                    calls.assert_not_called()

    def test_gpu_profile_accepts_only_one_full_l4(self):
        machine = {"MemoryInfo": {"SizeInMiB": 32768},
                   "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                   "GpuInfo": {"Gpus": [{"Name": "L4", "Manufacturer": "NVIDIA", "Count": 1,
                                       "MemoryInfo": {"SizeInMiB": 23040}}]}}
        vm.validate_machine(machine, "x86_64", "gpu")
        for change in ({"Name": "A10G"}, {"Manufacturer": "AMD"}, {"Count": 4},
                       {"GpuPartitionSize": 0.5}, {"MemoryInfo": {"SizeInMiB": 12000}}):
            wrong = copy.deepcopy(machine)
            wrong["GpuInfo"]["Gpus"][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "full NVIDIA L4"):
                vm.validate_machine(wrong, "x86_64", "gpu")
        del machine["GpuInfo"]
        with self.assertRaises(ValueError):
            vm.validate_machine(machine, "x86_64", "gpu")
        for memory, arch in ((16384, "x86_64"), (32768, "arm64")):
            machine["MemoryInfo"]["SizeInMiB"] = memory
            with self.assertRaises(ValueError):
                vm.validate_machine(machine, arch, "cpu")

    def test_unavailable_type_in_subnet_zone_fails_without_mutation(self):
        aws = vm.Aws("eu-central-1", None, False)
        with patch.object(aws, "call", return_value={"InstanceTypeOfferings": []}) as calls:
            with self.assertRaisesRegex(ValueError, "not offered in eu-central-1b"):
                vm.check_offering(aws, "g6.2xlarge", "eu-central-1b")
            self.assertEqual(calls.call_args.kwargs["location_type"], "availability-zone")
            self.assertIn({"Name": "location", "Values": ["eu-central-1b"]}, calls.call_args.kwargs["filters"])

    def test_existing_journal_blocks_second_launch_before_aws(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            vm.save_state(state, {"ApplyStatus": "started"})
            argv = ["rhel-vm", "apply", "--region", "eu-central-1", "--account-id", "123456789012",
                    "--state-file", str(state), "--config", str(ROOT / "configs/aws/vllm-gpu.json"), "--scenario", "all-in-one",
                    "--prefix", "test-qwen", "--subnet-id", "subnet-test", "--public-key", "missing.pub",
                    "--allowed-cidr", "192.0.2.1/32"]
            with patch.object(vm.sys, "argv", argv), patch.object(vm.Aws, "call") as calls:
                with self.assertRaisesRegex(ValueError, "incomplete journal"):
                    vm.main()
                calls.assert_not_called()

    def test_retry_apply_reuses_resources_and_client_token(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            proposal.update(Prefix=args.prefix, Scenario=args.scenario, SubnetId=args.subnet_id,
                            VolumeGiB=args.volume_gib, AvailabilityZone="eu-central-1a")
            state = {**proposal, "ClientToken": "original-token", "SecurityGroupId": "sg-original",
                     "KeyPairName": args.prefix, "ApplyStatus": "launch-requested"}
            vm.save_state(args.state_file, state)
            with patch.object(aws, "call", return_value={"Instances": [{"InstanceId": "i-retry"}]}) as calls, \
                    contextlib.redirect_stdout(io.StringIO()):
                vm.apply_retry(aws, args, state, state)
            self.assertEqual([call.args[1] for call in calls.call_args_list], ["run-instances"])
            self.assertEqual(calls.call_args.kwargs["client_token"], "original-token")
            self.assertEqual([row["ResourceType"] for row in calls.call_args.kwargs["tag_specifications"]],
                             ["instance", "volume"])
            self.assertEqual(calls.call_args.kwargs["network_interfaces"][0]["Groups"], ["sg-original"])
            self.assertEqual(json.loads(args.state_file.read_text())["InstanceId"], "i-retry")

    def test_new_launch_tag_contract_survives_retry_and_rejects_unknown_types(self):
        for tag_types in (["instance", "volume", "network-interface"], ["instance", "snapshot"]):
            with self.subTest(tag_types=tag_types), tempfile.TemporaryDirectory() as directory:
                args, proposal, aws = self.apply_inputs(directory)
                state = {**proposal, "ClientToken": "original-token", "SecurityGroupId": "sg-original",
                         "KeyPairName": args.prefix, "ApplyStatus": "launch-requested",
                         "LaunchTagResourceTypes": tag_types}
                vm.save_state(args.state_file, state)
                with patch.object(aws, "call", return_value={"Instances": [{"InstanceId": "i-retry"}]}) as calls, \
                        contextlib.redirect_stdout(io.StringIO()):
                    if "snapshot" in tag_types:
                        with self.assertRaisesRegex(ValueError, "launch-tag contract"):
                            vm.launch_instance(aws, args, proposal, state)
                        calls.assert_not_called()
                    else:
                        vm.launch_instance(aws, args, proposal, state)
                        self.assertEqual(calls.call_args.kwargs["client_token"], "original-token")
                        self.assertEqual([row["ResourceType"] for row in calls.call_args.kwargs["tag_specifications"]], tag_types)
                        self.assertEqual(json.loads(args.state_file.read_text())["LaunchTagResourceTypes"], tag_types)

    def retry_inputs(self, directory):
        args = self.settings(config=ROOT / "configs/aws/vllm-gpu.json", scenario="all-in-one",
            account_id="123456789012", region="eu-central-1", prefix="test-gpu", subnet_id="subnet-a",
            allowed_cidr="192.0.2.1/32", public_key=Path(directory) / "test.pub", state_file=Path(directory) / "retry.json")
        vm.configure(args)
        args.public_key.write_text("ssh-ed25519 fixture-public-key\n")
        state = {"AccountId": args.account_id, "Region": args.region, "Prefix": args.prefix,
            "Scenario": args.scenario, "Inference": args.inference, "InstanceType": args.instance_type,
            "Architecture": "x86_64", "VolumeGiB": args.volume_gib, "ImageId": "ami-aaaa",
            "RootDeviceName": "/dev/sda1", "SubnetId": args.subnet_id, "VpcId": "vpc-test", "AvailabilityZone": "eu-central-1a",
            "Ingress": vm.ingress(args.scenario, args.allowed_cidr), "ClientToken": "original-token",
            "SecurityGroupId": "sg-test", "KeyPairName": args.prefix, "ApplyStatus": "launch-requested"}
        vm.save_state(args.state_file, state)
        owned = vm.tags(args.prefix, args.scenario)
        replies = {
            "get-caller-identity": {"Account": args.account_id, "Arn": "arn:aws:iam::123456789012:user/test"},
            "describe-instances": {"Reservations": []},
            "describe-images": {"Images": [{"ImageId": state["ImageId"], "OwnerId": vm.OWNER, "Architecture": "x86_64",
                "State": "available", "RootDeviceType": "ebs", "RootDeviceName": "/dev/sda1",
                "Name": "RHEL-9.8_HVM-test-x86_64-0-Hourly2-GP3", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {}}]}]},
            "describe-subnets": {"Subnets": [{"SubnetId": args.subnet_id, "VpcId": "vpc-test", "OwnerId": args.account_id,
                "AvailabilityZone": "eu-central-1a", "AvailableIpAddressCount": 10, "State": "available"}]},
            "describe-instance-type-offerings": {"InstanceTypeOfferings": [{"Location": "eu-central-1a"}]},
            "describe-route-tables": {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-test", "State": "active"}]}]},
            "describe-security-groups": {"SecurityGroups": [{"GroupId": "sg-test", "GroupName": args.prefix,
                "VpcId": "vpc-test", "Tags": owned, "IpPermissions": state["Ingress"]}]},
            "describe-key-pairs": {"KeyPairs": [{"KeyName": args.prefix, "Tags": owned, "PublicKey": args.public_key.read_text()}]}}
        return args, state, replies

    def test_retry_plan_reconciles_resources_without_changing_the_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            args, state, replies = self.retry_inputs(directory)
            before = args.state_file.read_bytes()
            aws = vm.Aws(args.region, None, False)
            with patch.object(aws, "call", side_effect=lambda service, action, **options: replies[action]) as calls:
                proposal = vm.retry_plan(aws, args, vm.retry_state(args))
            self.assertEqual(proposal["ClientToken"], "original-token")
            self.assertIn("reuse", proposal["Action"])
            self.assertEqual(args.state_file.read_bytes(), before)
            self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

    def test_retry_refuses_changed_settings_and_uncertain_zone_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            args, state, _ = self.retry_inputs(directory)
            for field, value in (("instance_type", "g6.4xlarge"), ("volume_gib", 300), ("account_id", "000000000000"),
                                 ("allowed_cidr", "192.0.2.2/32"), ("subnet_id", "subnet-b")):
                changed = copy.copy(args)
                setattr(changed, field, value)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    vm.retry_state(changed)
            state["ApplyStatus"] = "capacity-unavailable"
            vm.update_state(args.state_file, state)
            args.subnet_id = "subnet-b"
            self.assertEqual(vm.retry_state(args), state)

    def test_retry_refuses_resource_drift_and_recovers_only_its_own_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            args, state, original = self.retry_inputs(directory)
            for case in ("ingress", "owner", "key", "instance"):
                replies = copy.deepcopy(original)
                if case == "ingress":
                    replies["describe-security-groups"]["SecurityGroups"][0]["IpPermissions"] = []
                elif case == "owner":
                    replies["describe-security-groups"]["SecurityGroups"][0]["Tags"] = []
                elif case == "key":
                    replies["describe-key-pairs"]["KeyPairs"][0]["PublicKey"] = "ssh-ed25519 different-key"
                else:
                    replies["describe-instances"] = {"Reservations": [{"Instances": [{"InstanceId": "i-other", "Tags": []}]}]}
                aws = vm.Aws(args.region, None, False)
                with self.subTest(case=case), patch.object(aws, "call", side_effect=lambda service, action, **options: replies[action]), \
                        self.assertRaises(ValueError):
                    vm.retry_plan(aws, args, state)
            original["describe-instances"] = {"Reservations": [{"Instances": [{"InstanceId": "i-found",
                "ClientToken": state["ClientToken"], "ImageId": state["ImageId"], "InstanceType": state["InstanceType"],
                "SubnetId": state["SubnetId"], "Tags": vm.tags(args.prefix, args.scenario)}]}]}
            with patch.object(aws, "call", side_effect=lambda service, action, **options: original[action]):
                self.assertEqual(vm.retry_plan(aws, args, state)["RecoveredInstanceId"], "i-found")

    def test_retry_zone_change_records_the_previous_attempt_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            args, state, _ = self.retry_inputs(directory)
            state["ApplyStatus"] = "capacity-unavailable"
            args.subnet_id = "subnet-b"
            proposal = {**state, "SubnetId": args.subnet_id, "AvailabilityZone": "eu-central-1b"}
            aws = vm.Aws(args.region, None, True)
            with patch.object(aws, "call", side_effect=ValueError("lost response")), \
                    self.assertRaises(ValueError):
                vm.apply_retry(aws, args, state, proposal)
            saved = json.loads(args.state_file.read_text())
            self.assertNotEqual(saved["ClientToken"], state["ClientToken"])
            self.assertEqual(saved["PreviousAttempts"][0]["ClientToken"], state["ClientToken"])
            self.assertEqual(saved["ApplyStatus"], "launch-requested")
            self.assertEqual(saved["SubnetId"], "subnet-b")

    def test_apply_lock_rejects_concurrent_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with vm.deployment_lock(path), self.assertRaisesRegex(ValueError, "another deployment"):
                with vm.deployment_lock(path):
                    self.fail("acquired the same deployment lock twice")

    def test_retry_recovers_a_lost_response_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            state = {**proposal, "ClientToken": "original-token", "ApplyStatus": "launch-requested"}
            vm.save_state(args.state_file, state)
            with patch.object(aws, "call") as calls, contextlib.redirect_stdout(io.StringIO()):
                vm.apply_retry(aws, args, state, {**state, "RecoveredInstanceId": "i-existing"})
            calls.assert_not_called()
            self.assertEqual(json.loads(args.state_file.read_text())["InstanceId"], "i-existing")

    def test_capacity_failure_is_recorded_for_explicit_zone_change(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            responses = [{"GroupId": "sg-test"}, {}, {}, vm.AwsError("InsufficientInstanceCapacity", "capacity")]
            with patch.object(aws, "call", side_effect=responses), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(vm.AwsError):
                vm.apply_plan(aws, args, proposal)
            self.assertEqual(json.loads(args.state_file.read_text())["ApplyStatus"], "capacity-unavailable")

    def test_apply_requires_confirmation_of_current_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["rhel-vm", "apply", "--region", "eu-central-1", "--account-id", "123456789012",
                    "--state-file", str(Path(directory) / "state.json"),
                    "--config", str(ROOT / "configs/aws/vllm-gpu.json"), "--scenario", "all-in-one", "--instance-type", "g6.4xlarge",
                    "--prefix", "test-qwen", "--subnet-id", "subnet-test", "--public-key", "missing.pub",
                    "--allowed-cidr", "192.0.2.1/32"]
            with patch.object(vm.sys, "argv", argv), patch.object(vm, "plan", return_value={}) as plan, \
                    patch.object(vm, "apply_plan") as apply, patch("builtins.input", return_value="no"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "confirmation did not match"):
                    vm.main()
                self.assertEqual(plan.call_args.args[1].instance_type, "g6.4xlarge")
                apply.assert_not_called()

    def test_obsolete_batch_invocation_cannot_plan_or_launch_without_config(self):
        # A previously sourced helper ignores NAME CONFIG and invokes this old CLI.
        for mode in ("plan", "apply"):
            argv = ["rhel-vm", mode, "--region", "eu-central-1", "--account-id", "123456789012",
                    "--state-file", "/unused/state.json", "--scenario", "all-in-one",
                    "--prefix", "old-all-in-one", "--subnet-id", "subnet-test", "--public-key", "missing.pub",
                    "--allowed-cidr", "192.0.2.1/32"]
            with patch.object(vm.sys, "argv", argv), patch.object(vm.Aws, "call") as calls:
                with self.assertRaisesRegex(ValueError, "--config.*reload"):
                    vm.main()
                calls.assert_not_called()

    def test_public_ssh_is_explicit_and_does_not_open_https_or_backends(self):
        for scenario in vm.SCENARIOS:
            if scenario in vm.AUXILIARY:
                with self.assertRaises(ValueError):
                    vm.ingress(scenario, "192.0.2.1/32", ssh_access="public")
                continue
            rules = vm.ingress(scenario, "192.0.2.1/32", ssh_access="public")
            self.assertEqual(rules[0]["IpRanges"], [{"CidrIp": "0.0.0.0/0"}])
            self.assertEqual([rule["FromPort"] for rule in rules],
                             [22, 8443] if scenario == "remote-gateway" else [22])
            if scenario == "remote-gateway":
                self.assertEqual(rules[1]["IpRanges"], [{"CidrIp": "192.0.2.1/32"}])
        self.assertEqual(len(vm.ingress("all-in-one", None, ssh_access="public")), 1)
        for scenario, cidr, access in (("remote-gateway", None, "public"),
                ("remote-gateway", "0.0.0.0/0", "public"), ("all-in-one", None, "restricted"),
                ("all-in-one", "192.0.2.1/32", "typo")):
            with self.subTest(scenario=scenario, cidr=cidr, access=access), self.assertRaises(ValueError):
                vm.ingress(scenario, cidr, ssh_access=access)

    def test_https_access_is_independent_and_remote_only(self):
        for ssh_access in ("restricted", "public"):
            for https_access in ("restricted", "public"):
                rules = vm.ingress("remote-gateway", "192.0.2.1/32", ssh_access, https_access)
                self.assertEqual([rule["FromPort"] for rule in rules], [22, 8443])
                self.assertEqual([rule["IpRanges"] for rule in rules], [
                    [{"CidrIp": "0.0.0.0/0" if access == "public" else "192.0.2.1/32"}]
                    for access in (ssh_access, https_access)])
        self.assertEqual(len(vm.ingress("remote-gateway", None, "public", "public")), 2)
        for scenario, cidr, ssh_access, https_access in (
                ("remote-gateway", None, "restricted", "public"),
                ("remote-gateway", "0.0.0.0/0", "restricted", "public"),
                ("remote-gateway", "192.0.2.1/32", "restricted", "typo"),
                ("all-in-one", "192.0.2.1/32", "restricted", "public")):
            with self.subTest(scenario=scenario, cidr=cidr), self.assertRaises(ValueError):
                vm.ingress(scenario, cidr, ssh_access, https_access)

    def test_https_access_config_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "vm.json"
            config.write_text(json.dumps({"scenario": "remote-gateway", "https_access": "public"}))
            args = self.settings(config=config)
            vm.configure(args)
            self.assertEqual(args.https_access, "public")
            self.assertEqual(args.ssh_access, "restricted")
            args = self.settings(config=config, https_access="restricted")
            vm.configure(args)
            self.assertEqual(args.https_access, "restricted")
            with self.assertRaisesRegex(ValueError, "https_access"):
                vm.configure(self.settings(config=config, https_access="typo"))
            with self.assertRaisesRegex(ValueError, "remote-gateway"):
                vm.configure(self.settings(config=config, scenario="all-in-one"))

    def test_ssh_access_config_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "vm.json"
            config.write_text(json.dumps({"scenario": "all-in-one", "ssh_access": "public"}))
            args = self.settings(config=config)
            vm.configure(args)
            self.assertEqual(args.ssh_access, "public")
            args = self.settings(config=config, ssh_access="restricted")
            vm.configure(args)
            self.assertEqual(args.ssh_access, "restricted")
            with self.assertRaisesRegex(ValueError, "ssh_access"):
                vm.configure(self.settings(config=config, ssh_access="typo"))

    def test_wrong_account_and_root_rejected(self):
        for identity in [{"Account": "000000000000", "Arn": "arn:aws:iam::000000000000:user/test"},
                         {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root"}]:
            aws = vm.Aws("eu-central-1", None, False)
            with patch.object(aws, "call", return_value=identity), self.assertRaises(ValueError):
                vm.check_identity(aws, "123456789012")

    def test_ingress_is_explicit_and_scenario_specific(self):
        self.assertEqual([rule["FromPort"] for rule in vm.ingress("all-in-one", "192.0.2.1/32")], [22])
        self.assertEqual([rule["FromPort"] for rule in vm.ingress("remote-gateway", "192.0.2.1/32")], [22, 8443])
        self.assertEqual([rule["FromPort"] for rule in vm.ingress("vllm-server", "192.0.2.1/32")], [22])
        with self.assertRaises(ValueError):
            vm.ingress("typo", "192.0.2.1/32")
        for bad in ["0.0.0.0/0", "192.0.2.0/24", "::/0", "bad"]:
            with self.assertRaises(ValueError):
                vm.ingress("remote-gateway", bad)

    def endpoint_args(self, **overrides):
        values = {"account_id": "123456789012", "region": "eu-central-1",
                  "vllm_prefix": "gateway-test-vllm-server", "vllm_instance_id": None,
                  "client_vpc_id": "vpc-test", "client_state_file": None}
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_vllm_endpoint_discovers_one_private_address_and_requires_source_group_ingress(self):
        with tempfile.TemporaryDirectory() as directory:
            client_state = {"AccountId": "123456789012", "Region": "eu-central-1",
                            "Prefix": "gateway-test-all-in-one", "Scenario": "all-in-one",
                            "InstanceId": "i-client", "SecurityGroupId": "sg-client",
                            "Ingress": vm.ingress("all-in-one", "192.0.2.1/32")}
            client_state_path = Path(directory) / "client.json"
            vm.save_state(client_state_path, client_state)
            client = {"InstanceId": "i-client", "State": {"Name": "running"}, "VpcId": "vpc-test",
                      "Tags": vm.tags(client_state["Prefix"], client_state["Scenario"]),
                      "SecurityGroups": [{"GroupId": "sg-client"}]}
            instance = {"InstanceId": "i-vllm", "PrivateIpAddress": "10.0.1.10", "VpcId": "vpc-test",
                        "Tags": vm.tags("gateway-test-vllm-server", "vllm-server"),
                        "SecurityGroups": [{"GroupId": "sg-vllm"}]}
            group = {"GroupId": "sg-vllm", "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 8000,
                        "ToPort": 8000, "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]}
            group["Tags"] = vm.tags("gateway-test-vllm-server", "vllm-server")
            identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}
            client_reply = {"Reservations": [{"Instances": [client]}]}
            vllm_reply = {"Reservations": [{"Instances": [instance]}]}
            replies = [identity, client_reply, vllm_reply, {"SecurityGroups": [group]}]
            aws = vm.Aws("eu-central-1", None, False)
            endpoint_args = self.endpoint_args(client_state_file=client_state_path)
            with patch.object(aws, "call", side_effect=replies) as calls, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                vm.vllm_endpoint(aws, endpoint_args)
            result = json.loads(output.getvalue())
            self.assertEqual(result["VllmEndpoint"], "10.0.1.10:8000")
            self.assertEqual(result["VpcId"], "vpc-test")
            self.assertEqual([call.args[1] for call in calls.call_args_list],
                             ["get-caller-identity", "describe-instances", "describe-instances",
                              "describe-security-groups"])
            self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

            public_groups = []
            for permission in ({"IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                               {"Ipv6Ranges": [{"CidrIpv6": "::/0"}]},
                               {"IpRanges": [{"CidrIp": "0.0.0.0/1"}, {"CidrIp": "128.0.0.0/1"}]},
                               {"Ipv6Ranges": [{"CidrIpv6": "2600::/23"}]},
                               {"IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
                               {"IpRanges": [{"CidrIp": "172.16.0.0/12"}]},
                               {"IpRanges": [{"CidrIp": "192.168.0.0/16"}]},
                               {"Ipv6Ranges": [{"CidrIpv6": "fc00::/7"}]},
                               {"PrefixListIds": [{"PrefixListId": "pl-public"}]}):
                bad_group = copy.deepcopy(group)
                bad_group["IpPermissions"][0]["UserIdGroupPairs"] = []
                bad_group["IpPermissions"][0].pop("IpRanges", None)
                bad_group["IpPermissions"][0].pop("Ipv6Ranges", None)
                bad_group["IpPermissions"][0].update(permission)
                public_groups.append(bad_group)
            for bad_group in public_groups:
                with patch.object(aws, "call", side_effect=[identity, client_reply, vllm_reply,
                                                            {"SecurityGroups": [bad_group]}]), \
                        self.assertRaisesRegex(ValueError, "port 8000 ingress"):
                    vm.vllm_endpoint(aws, endpoint_args)

            all_protocol_group = copy.deepcopy(group)
            all_protocol_group["IpPermissions"] = [{"IpProtocol": "-1",
                                                    "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]
            with patch.object(aws, "call", side_effect=[identity, client_reply, vllm_reply,
                                                        {"SecurityGroups": [all_protocol_group]}]), \
                    self.assertRaisesRegex(ValueError, "exact TCP source-group rule"):
                vm.vllm_endpoint(aws, endpoint_args)

            missing_prefix_instance = copy.deepcopy(instance)
            missing_prefix_instance["Tags"] = [tag for tag in missing_prefix_instance["Tags"]
                                               if tag["Key"] != "ResourcePrefix"]
            with patch.object(aws, "call", side_effect=[identity, client_reply,
                                                        {"Reservations": [{"Instances": [missing_prefix_instance]}]}]), \
                    self.assertRaisesRegex(ValueError, "instance ownership"):
                vm.vllm_endpoint(aws, endpoint_args)

            unmanaged_group = copy.deepcopy(group)
            unmanaged_group["Tags"] = vm.tags("attacker", "vllm-server")
            with patch.object(aws, "call", side_effect=[identity, client_reply, vllm_reply,
                                                        {"SecurityGroups": [unmanaged_group]}]), \
                    self.assertRaisesRegex(ValueError, "security-group ownership"):
                vm.vllm_endpoint(aws, endpoint_args)

            other_client_group = copy.deepcopy(group)
            other_client_group["IpPermissions"][0]["UserIdGroupPairs"] = [{"GroupId": "sg-other"}]
            with patch.object(aws, "call", side_effect=[identity, client_reply, vllm_reply,
                                                        {"SecurityGroups": [other_client_group]}]), \
                    self.assertRaisesRegex(ValueError, "verified client has no source-group access"):
                vm.vllm_endpoint(aws, endpoint_args)

            with patch.object(aws, "call", side_effect=[identity, client_reply, {"Reservations": []}]), \
                    self.assertRaisesRegex(ValueError, "exactly one"):
                vm.vllm_endpoint(aws, endpoint_args)
            non_rfc1918_instance = copy.deepcopy(instance)
            non_rfc1918_instance["PrivateIpAddress"] = "100.64.1.10"
            with patch.object(aws, "call", side_effect=[identity, client_reply,
                                                        {"Reservations": [{"Instances": [non_rfc1918_instance]}]}]), \
                    self.assertRaisesRegex(ValueError, "RFC1918"):
                vm.vllm_endpoint(aws, endpoint_args)
            other_vpc_instance = copy.deepcopy(instance)
            other_vpc_instance["VpcId"] = "vpc-other"
            with patch.object(aws, "call", side_effect=[identity, client_reply,
                                                        {"Reservations": [{"Instances": [other_vpc_instance]}]}]), \
                    self.assertRaisesRegex(ValueError, "vLLM instance is not"):
                vm.vllm_endpoint(aws, endpoint_args)

    def test_vllm_grant_uses_source_group_and_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_state = {"AccountId": "123456789012", "Region": "eu-central-1",
                            "Prefix": "gateway-test-all-in-one", "Scenario": "all-in-one",
                            "InstanceId": "i-client", "SecurityGroupId": "sg-client",
                            "Ingress": vm.ingress("all-in-one", "192.0.2.1/32")}
            vllm_state = {"AccountId": "123456789012", "Region": "eu-central-1",
                          "Prefix": "gateway-test-vllm-server", "Scenario": "vllm-server",
                          "InstanceId": "i-vllm", "SecurityGroupId": "sg-vllm",
                          "Ingress": vm.ingress("vllm-server", "192.0.2.1/32")}
            client_state_path = root / "client.json"
            vllm_state_path = root / "vllm.json"
            vm.save_state(client_state_path, client_state)
            vm.save_state(vllm_state_path, vllm_state)
            client = {"InstanceId": "i-client", "State": {"Name": "running"}, "VpcId": "vpc-test",
                      "Tags": vm.tags(client_state["Prefix"], client_state["Scenario"]),
                      "SecurityGroups": [{"GroupId": "sg-client"}]}
            vllm = {"InstanceId": "i-vllm", "State": {"Name": "running"}, "VpcId": "vpc-test",
                    "Tags": vm.tags(vllm_state["Prefix"], vllm_state["Scenario"]),
                    "SecurityGroups": [{"GroupId": "sg-vllm"}]}
            client_group = {"GroupId": "sg-client"}
            client_group["Tags"] = vm.tags(client_state["Prefix"], client_state["Scenario"])
            client_group["IpPermissions"] = copy.deepcopy(client_state["Ingress"])
            group = {"GroupId": "sg-vllm", "IpPermissions": []}
            group["Tags"] = vm.tags(vllm_state["Prefix"], vllm_state["Scenario"])
            group["IpPermissions"] = copy.deepcopy(vllm_state["Ingress"])
            identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}
            args = SimpleNamespace(account_id="123456789012", region="eu-central-1",
                                   client_state_file=root / "client.json",
                                   vllm_state_file=root / "vllm.json", apply=False)
            aws = vm.Aws(args.region, None, False)
            tampered_client_state = {**client_state, "Scenario": "attacker"}
            tampered_client_path = root / "tampered-client.json"
            vm.save_state(tampered_client_path, tampered_client_state)
            tampered_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                            client_state_file=tampered_client_path,
                                            vllm_state_file=vllm_state_path, apply=True)
            with patch.object(aws, "call", return_value=identity) as calls:
                with self.assertRaisesRegex(ValueError, "supported non-vLLM single server"):
                    vm.vllm_grant(aws, tampered_args)
            calls.assert_called_once()

            client_reply = {"Reservations": [{"Instances": [client]}]}
            vllm_reply = {"Reservations": [{"Instances": [vllm]}]}
            client_group_reply = {"SecurityGroups": [client_group]}
            vllm_group_reply = {"SecurityGroups": [group]}
            replies = [identity, client_reply, vllm_reply, client_group_reply, vllm_group_reply]
            tampered_client_group = copy.deepcopy(client_group)
            tampered_client_group["Tags"] = vm.tags("attacker", client_state["Scenario"])
            with patch.object(aws, "call", side_effect=[identity, client_reply, vllm_reply,
                                                        {"SecurityGroups": [tampered_client_group]}]) as calls:
                with self.assertRaisesRegex(ValueError, "client security-group ownership"):
                    vm.vllm_grant(aws, args)
            self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

            with patch.object(aws, "call", side_effect=replies) as calls, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                vm.vllm_grant(aws, args)
            self.assertIn('"Exists": false', output.getvalue())
            self.assertEqual([call.args[1] for call in calls.call_args_list],
                             ["get-caller-identity", "describe-instances", "describe-instances",
                              "describe-security-groups", "describe-security-groups"])

            wrong_group = copy.deepcopy(group)
            wrong_group["Tags"] = vm.tags(client_state["Prefix"], client_state["Scenario"])
            with patch.object(aws, "call", side_effect=[*replies[:4],
                                                        {"SecurityGroups": [wrong_group]}]), \
                    self.assertRaisesRegex(ValueError, "security-group ownership"):
                vm.vllm_grant(aws, args)

            args.apply = True
            aws = vm.Aws(args.region, None, False)
            confirmation = "grant 123456789012 eu-central-1 sg-vllm sg-client"
            with patch.object(aws, "call", side_effect=[*replies,
                                                        {"SecurityGroups": [group]}, {}]) as calls, \
                    patch("builtins.input", return_value=confirmation), \
                    contextlib.redirect_stdout(io.StringIO()):
                vm.vllm_grant(aws, args)
            mutation = calls.call_args_list[-1]
            self.assertEqual(mutation.args[1], "authorize-security-group-ingress")
            self.assertEqual(mutation.kwargs["group_id"], "sg-vllm")
            self.assertEqual(mutation.kwargs["ip_permissions"][0]["UserIdGroupPairs"],
                             [{"GroupId": "sg-client",
                               "Description": "secure-single-server Praxis client"}])
            self.assertEqual(json.loads(vllm_state_path.read_text()).get("ManagedGrants"), ["sg-client"])

            rule_group = {"GroupId": "sg-vllm", "IpPermissions": [
                *vllm_state["Ingress"],
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                 "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]}
            rule_group["Tags"] = group["Tags"]
            duplicate = vm.AwsError("InvalidPermission.Duplicate", "rule already exists")
            vm.update_state(vllm_state_path, vllm_state)
            with patch.object(aws, "call", side_effect=[*replies,
                                                        {"SecurityGroups": [group]},
                                                        duplicate,
                                                        {"SecurityGroups": [rule_group]}]) as calls, \
                    patch("builtins.input", return_value=confirmation), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                vm.vllm_grant(aws, args)
            self.assertEqual(calls.call_args_list[-1].args[1], "describe-security-groups")
            self.assertIn("Granted private TCP 8000 access", output.getvalue())
            self.assertEqual(json.loads(vllm_state_path.read_text()).get("ManagedGrants"), ["sg-client"])
            vm.update_state(vllm_state_path, vllm_state)

            stale_state = dict(vllm_state)
            stale_state["ManagedGrants"] = ["sg-client"]
            stale_path = root / "stale.json"
            vm.save_state(stale_path, stale_state)
            stale_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                         client_state_file=client_state_path,
                                         vllm_state_file=stale_path, apply=True)
            with patch.object(aws, "call", side_effect=[identity, replies[1], replies[2], client_group_reply,
                                                        {"SecurityGroups": [group]}]) as calls, \
                    patch("builtins.input", return_value=confirmation), \
                    self.assertRaisesRegex(ValueError, "managed vLLM grants differ"):
                vm.vllm_grant(aws, stale_args)
            self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

            drifted_group = copy.deepcopy(group)
            drifted_group["Tags"] = vm.tags("attacker", "vllm-server")
            drift_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                         client_state_file=client_state_path,
                                         vllm_state_file=vllm_state_path, apply=True)
            with patch.object(aws, "call", side_effect=[identity, replies[1], replies[2], client_group_reply,
                                                        {"SecurityGroups": [group]},
                                                        {"SecurityGroups": [drifted_group]}]) as calls, \
                    patch("builtins.input", return_value=confirmation), \
                    self.assertRaisesRegex(ValueError, "security-group ownership"):
                vm.vllm_grant(aws, drift_args)
            self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))

            public_group = copy.deepcopy(group)
            public_group["IpPermissions"] = [
                *vllm_state["Ingress"],
                {"IpProtocol": "tcp", "FromPort": vm.VLLM_PORT, "ToPort": vm.VLLM_PORT,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
            public_state = {**vllm_state,
                            "Ingress": copy.deepcopy(public_group["IpPermissions"])}
            with patch.object(aws, "call", return_value={"SecurityGroups": [public_group]}):
                with self.assertRaisesRegex(ValueError, "port 8000 ingress"):
                    vm.validate_vllm_grant_state(aws, public_state, "sg-client")

            all_protocol_group = {"GroupId": "sg-vllm", "IpPermissions": [{"IpProtocol": "-1",
                                    "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]}
            self.assertFalse(vm.source_group_grant_exists(all_protocol_group, "sg-client"))
            all_protocol_state = {**vllm_state, "ManagedGrants": ["sg-client"]}
            with self.assertRaisesRegex(ValueError, "ingress"):
                vm.check_ingress(all_protocol_group, all_protocol_state)

            broad_private_group = {"GroupId": "sg-vllm", "IpPermissions": [
                *vllm_state["Ingress"],
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                 "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]}
            with self.assertRaisesRegex(ValueError, "ingress differs"):
                vm.check_ingress(broad_private_group, vllm_state)
            broad_private_group["Tags"] = group["Tags"]
            broad_private_state = {**vllm_state, "Ingress": copy.deepcopy(broad_private_group["IpPermissions"])}
            with patch.object(aws, "call", return_value={"SecurityGroups": [broad_private_group]}):
                with self.assertRaisesRegex(ValueError, "port 8000 ingress"):
                    vm.validate_vllm_grant_state(aws, broad_private_state, "sg-client")

            extra_group_state = {**vllm_state, "ManagedGrants": ["sg-client"]}
            extra_group_rule = {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                                "UserIdGroupPairs": [{"GroupId": "sg-client"}, {"GroupId": "sg-other"}]}
            extra_group = {"GroupId": "sg-vllm", "IpPermissions": [
                *vllm_state["Ingress"], extra_group_rule]}
            with self.assertRaisesRegex(ValueError, "unexpected non-IPv4 ingress"):
                vm.check_ingress(extra_group, extra_group_state)
            tracked_group_state = {**vllm_state, "ManagedGrants": ["sg-client", "sg-other"]}
            vm.check_ingress(extra_group, tracked_group_state)

            missing_grant_state = {**vllm_state, "ManagedGrants": ["sg-client", "sg-other"]}
            missing_grant_group = {"GroupId": "sg-vllm", "IpPermissions": [
                *vllm_state["Ingress"],
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                 "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]}
            with self.assertRaisesRegex(ValueError, "managed vLLM grants differ"):
                vm.check_ingress(missing_grant_group, missing_grant_state)

            missing_journal_state = dict(vllm_state)
            missing_journal_path = root / "missing-journal.json"
            vm.save_state(missing_journal_path, missing_journal_state)
            missing_journal_group = {"GroupId": "sg-vllm", "Tags": group["Tags"], "IpPermissions": [
                *vllm_state["Ingress"],
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                 "UserIdGroupPairs": [{"GroupId": "sg-client"}]}]}
            missing_journal_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                                   client_state_file=client_state_path,
                                                   vllm_state_file=missing_journal_path, apply=True)
            with patch.object(aws, "call", side_effect=[identity, replies[1], replies[2], client_group_reply,
                                                        {"SecurityGroups": [missing_journal_group]}]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                vm.vllm_grant(aws, missing_journal_args)
            self.assertIn("already granted", output.getvalue())
            self.assertEqual(json.loads(missing_journal_path.read_text())["ManagedGrants"], ["sg-client"])

            malformed = root / "malformed.json"
            malformed.write_text("[]")
            malformed_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                             client_state_file=malformed)
            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                vm.launch_state(malformed, malformed_args)

            incomplete = root / "incomplete.json"
            incomplete.write_text(json.dumps({key: value for key, value in client_state.items()
                                               if key != "Prefix"}))
            incomplete_args = SimpleNamespace(account_id=args.account_id, region=args.region,
                                              client_state_file=incomplete)
            with self.assertRaisesRegex(ValueError, "incomplete launch journal"):
                vm.launch_state(incomplete, incomplete_args)

            aws = vm.Aws(args.region, None, False)
            with patch.object(aws, "call", return_value={"Reservations": []}) as calls:
                with self.assertRaisesRegex(ValueError, "instance was not found"):
                    vm.described_instance(aws, client_state, client_state["Scenario"])
                calls.assert_called_once()

    def test_missing_public_key_fails_before_aws_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(public_key=Path(directory) / "missing.pub")
            aws = vm.Aws("eu-central-1", None, False)
            with patch.object(aws, "call") as calls, self.assertRaisesRegex(ValueError, "public key.*aws_test_key"):
                vm.plan(aws, args)
            calls.assert_not_called()

    def test_read_only_guard_precedes_subprocess(self):
        aws = vm.Aws("eu-central-1", None, False)
        with patch.object(vm.subprocess, "run") as run:
            for service, action in [("ec2", "run-instances"), ("ec2", "create-security-group"),
                                    ("iam", "create-role"), ("ec2", "terminate-instances")]:
                with self.assertRaises(ValueError):
                    aws.call(service, action)
            run.assert_not_called()

    def test_architecture_and_ami(self):
        for architecture in ["x86_64", "arm64"]:
            image = {"OwnerId": vm.OWNER, "Architecture": architecture, "RootDeviceType": "ebs",
                     "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {}}],
                     "State": "available", "Name": f"RHEL-9.6_HVM-test-{architecture}-Hourly2-GP3"}
            vm.validate_image(image, architecture)
            with self.assertRaises(ValueError):
                vm.validate_image(image, "arm64" if architecture == "x86_64" else "x86_64")
            image["OwnerId"] = "untrusted"
            with self.assertRaises(ValueError):
                vm.validate_image(image, architecture)

    def test_image_cannot_add_unplanned_disks(self):
        image = {"OwnerId": vm.OWNER, "Architecture": "arm64", "RootDeviceType": "ebs",
                 "RootDeviceName": "/dev/sda1", "State": "available",
                 "Name": "RHEL-9.6_HVM-test-arm64-Hourly2-GP3"}
        for mappings in [[], [{"DeviceName": "/dev/sdb", "Ebs": {}}],
                         [{"DeviceName": "/dev/sda1", "Ebs": {}}, {"DeviceName": "/dev/sdb", "Ebs": {}}]]:
            with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                vm.validate_image({**image, "BlockDeviceMappings": mappings}, "arm64")

    def test_state_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            vm.save_state(path, {"InstanceId": "i-test"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                vm.save_state(path, {})

    def apply_inputs(self, directory):
        root = Path(directory)
        args = SimpleNamespace(prefix="test-remote", scenario="remote-gateway",
            public_key=root / "test.pub", subnet_id="subnet-test", volume_gib=50,
            state_file=root / "state.json")
        proposal = {"VpcId": "vpc-test", "Ingress": vm.ingress(args.scenario, "192.0.2.1/32"),
            "ImageId": "ami-test", "InstanceType": "m7i.2xlarge", "RootDeviceName": "/dev/sda1"}
        return args, proposal, vm.Aws("eu-central-1", None, True)

    def test_bad_state_path_prevents_every_aws_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            blocker = Path(directory) / "not-a-directory"
            blocker.touch()
            args.state_file = blocker / "state.json"
            with patch.object(aws, "call", return_value={}) as calls, self.assertRaises(OSError):
                vm.apply_plan(aws, args, proposal)
            calls.assert_not_called()

    def test_state_sync_failure_prevents_every_aws_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            with patch.object(vm.os, "fsync", side_effect=OSError("disk failure")), \
                    patch.object(aws, "call", return_value={}) as calls, self.assertRaises(OSError):
                vm.apply_plan(aws, args, proposal)
            calls.assert_not_called()

    def test_apply_journals_before_launch_and_deletes_attached_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            args.volume_gib = 300
            proposal["InstanceType"] = "g6.4xlarge"
            operations = []
            def respond(service, action, **options):
                operations.append(action)
                state = json.loads(args.state_file.read_text())
                self.assertEqual(args.state_file.stat().st_mode & 0o777, 0o600)
                self.assertTrue(state["ClientToken"])
                if action == "create-security-group":
                    return {"GroupId": "sg-test"}
                self.assertEqual(state["SecurityGroupId"], "sg-test")
                if action == "run-instances":
                    self.assertEqual(state["ApplyStatus"], "launch-requested")
                    self.assertEqual(state["KeyPairName"], args.prefix)
                    self.assertEqual(options["client_token"], state["ClientToken"])
                    self.assertEqual(options["count"], 1)
                    self.assertEqual(options["instance_type"], "g6.4xlarge")
                    self.assertEqual(options["block_device_mappings"][0]["Ebs"]["VolumeSize"], 300)
                    self.assertEqual(len(options["network_interfaces"]), 1)
                    self.assertTrue(options["network_interfaces"][0]["DeleteOnTermination"])
                    self.assertTrue(options["network_interfaces"][0]["AssociatePublicIpAddress"])
                    self.assertEqual([row["ResourceType"] for row in options["tag_specifications"]],
                                     ["instance", "volume", "network-interface"])
                    self.assertTrue(all(row["Tags"] == vm.tags(args.prefix, args.scenario)
                                        for row in options["tag_specifications"]))
                    self.assertEqual(len(options["block_device_mappings"]), 1)
                    self.assertTrue(options["block_device_mappings"][0]["Ebs"]["DeleteOnTermination"])
                    return {"Instances": [{"InstanceId": "i-test"}]}
                return {}
            with patch.object(aws, "call", side_effect=respond), contextlib.redirect_stdout(io.StringIO()):
                vm.apply_plan(aws, args, proposal)
            final = json.loads(args.state_file.read_text())
            self.assertEqual(final["InstanceId"], "i-test")
            self.assertEqual(final["ApplyStatus"], "complete")
            self.assertEqual(operations, ["create-security-group", "authorize-security-group-ingress",
                                          "import-key-pair", "run-instances"])

    def test_launch_failure_keeps_recovery_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            args, proposal, aws = self.apply_inputs(directory)
            responses = [{"GroupId": "sg-test"}, {}, {}, ValueError("launch response lost")]
            with patch.object(aws, "call", side_effect=responses), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                vm.apply_plan(aws, args, proposal)
            state = json.loads(args.state_file.read_text())
            self.assertEqual(state["SecurityGroupId"], "sg-test")
            self.assertEqual(state["KeyPairName"], args.prefix)
            self.assertEqual(state["ApplyStatus"], "launch-requested")
            self.assertTrue(state["ClientToken"])
            self.assertNotIn("InstanceId", state)

    def test_failed_journal_update_preserves_previous_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            vm.save_state(path, {"ApplyStatus": "started"})
            with patch.object(vm.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                vm.update_state(path, {"ApplyStatus": "complete"})
            self.assertEqual(json.loads(path.read_text()), {"ApplyStatus": "started"})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_verify_rejects_retained_network_interface_or_extra_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(account_id="123456789012", region="eu-central-1",
                                   state_file=Path(directory) / "state.json")
            state = {"AccountId": args.account_id, "Region": args.region, "Prefix": "test-remote",
                "Scenario": "remote-gateway", "InstanceId": "i-test", "ImageId": "ami-test",
                "InstanceType": "m7i.2xlarge", "Architecture": "x86_64", "SecurityGroupId": "sg-test",
                "RootDeviceName": "/dev/sda1", "VolumeGiB": 50,
                "Ingress": vm.ingress("remote-gateway", "192.0.2.1/32")}
            vm.save_state(args.state_file, state)
            owned = vm.tags(state["Prefix"], state["Scenario"])
            instance = {"InstanceId": "i-test", "ImageId": "ami-test", "InstanceType": state["InstanceType"],
                "Tags": owned, "State": {"Name": "running"}, "SecurityGroups": [{"GroupId": "sg-test"}],
                "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"},
                "NetworkInterfaces": [{"Attachment": {"DeleteOnTermination": True}}],
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeId": "vol-test", "DeleteOnTermination": True}}]}
            image = {"OwnerId": vm.OWNER, "Architecture": "x86_64", "RootDeviceType": "ebs",
                "State": "available", "Name": "RHEL-9.6_HVM-test-x86_64-Hourly2-GP3",
                "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {}}]}
            aws = vm.Aws(args.region, None, False)
            responses = [{"Account": args.account_id, "Arn": "arn:aws:iam::123456789012:user/test"},
                {"Reservations": [{"Instances": [instance]}]}, {"Images": [image]},
                {"SecurityGroups": [{"Tags": owned, "IpPermissions": state["Ingress"]}]},
                {"Volumes": [{"Encrypted": True, "Size": 50}]}]
            with patch.object(aws, "call", side_effect=responses) as calls, contextlib.redirect_stdout(io.StringIO()):
                vm.verify(aws, args)
                self.assertTrue(all(call.args[1] in vm.READS for call in calls.call_args_list))
            state["ManagedGrants"] = ["sg-client"]
            vm.update_state(args.state_file, state)
            managed_rule = {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                            "UserIdGroupPairs": [{"GroupId": "sg-client"}]}
            responses[3]["SecurityGroups"][0]["IpPermissions"].append(managed_rule)
            with patch.object(aws, "call", side_effect=responses), self.assertRaisesRegex(ValueError, "ingress"):
                vm.verify(aws, args)
            vllm_grant_state = {**state, "Scenario": "vllm-server",
                                "Ingress": vm.ingress("vllm-server", "192.0.2.1/32")}
            vm.check_ingress({"IpPermissions": [*vllm_grant_state["Ingress"], managed_rule]},
                             vllm_grant_state)
            managed_rule["UserIdGroupPairs"] = [{"GroupId": "sg-unrelated"}]
            with patch.object(aws, "call", side_effect=responses), self.assertRaisesRegex(ValueError, "ingress"):
                vm.verify(aws, args)
            responses[3]["SecurityGroups"][0]["IpPermissions"] = copy.deepcopy(state["Ingress"])
            del state["ManagedGrants"]
            vm.update_state(args.state_file, state)
            # Public SSH must remain verifiable without accepting public HTTPS drift.
            state["SshAccess"] = "public"
            state["Ingress"] = vm.ingress("remote-gateway", "192.0.2.1/32", ssh_access="public")
            vm.update_state(args.state_file, state)
            group = responses[3]["SecurityGroups"][0]
            group["IpPermissions"] = copy.deepcopy(state["Ingress"])
            with patch.object(aws, "call", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
                vm.verify(aws, args)
            group["IpPermissions"][1]["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
            with patch.object(aws, "call", side_effect=responses), self.assertRaisesRegex(ValueError, "ingress"):
                vm.verify(aws, args)
            group["IpPermissions"] = copy.deepcopy(state["Ingress"])
            instance["NetworkInterfaces"][0]["Attachment"]["DeleteOnTermination"] = False
            with patch.object(aws, "call", side_effect=responses), self.assertRaisesRegex(ValueError, "network interface"):
                vm.verify(aws, args)
            instance["NetworkInterfaces"][0]["Attachment"]["DeleteOnTermination"] = True
            instance["BlockDeviceMappings"].append({"DeviceName": "/dev/sdb"})
            with patch.object(aws, "call", side_effect=responses), self.assertRaisesRegex(ValueError, "extra disks"):
                vm.verify(aws, args)


if __name__ == "__main__":
    unittest.main()
