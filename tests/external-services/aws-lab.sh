#!/usr/bin/env bash
# Source after scripts/aws/session.sh. No AWS calls on load.

aws_lab_plan() {
  aws_test_plan gateway "$AWS_TEST_REPO/tests/external-services/gateway-vm.json" --scenario remote-gateway --ssh-access restricted --https-access restricted &&
  aws_test_plan keycloak "$AWS_TEST_REPO/tests/external-services/keycloak-vm.json" --scenario keycloak --ssh-access restricted --https-access restricted &&
  aws_test_plan observability "$AWS_TEST_REPO/tests/external-services/observability-vm.json" --scenario observability --ssh-access restricted --https-access restricted
}

aws_lab_deploy() {
  # Preserve separate confirmation and recovery journal for every billable VM.
  # On partial failure resume the specific role, never relaunch the full batch.
  local lab_status=0
  aws_lab_plan &&
  aws_test_deploy gateway "$AWS_TEST_REPO/tests/external-services/gateway-vm.json" --scenario remote-gateway --ssh-access restricted --https-access restricted &&
  aws_test_deploy keycloak "$AWS_TEST_REPO/tests/external-services/keycloak-vm.json" --scenario keycloak --ssh-access restricted --https-access restricted &&
  aws_test_deploy observability "$AWS_TEST_REPO/tests/external-services/observability-vm.json" --scenario observability --ssh-access restricted --https-access restricted || lab_status=$?
  # Keep recovery information visible even after a partial launch failure.
  aws_lab_summary || return 1
  return "$lab_status"
}

aws_lab_summary() {
  if [[ -z "${RUN_PREFIX:-}" ]]; then
    printf 'No deployment selected; no recovery journal summary available.\n' >&2
    return 0
  fi
  local lab_manifest="$AWS_TEST_REPO/.state/deployments/$RUN_PREFIX.json"
  if [[ -f "$lab_manifest" ]]; then
    python3 "$AWS_TEST_REPO/tests/external-services/deployments.py" summary \
      "$lab_manifest" --journals "$AWS_TEST_REPO/.state"
  else
    printf 'Recovery journals: %s/.state/%s-<role>.json\n' "$AWS_TEST_REPO" "$RUN_PREFIX"
    printf 'No registry manifest: retain each VM journal for recovery and ownership verification.\n'
  fi
}

aws_lab_info() {
  local lab_role lab_instance lab_journal lab_public lab_private lab_prefix
  for lab_role in gateway keycloak observability; do
    lab_journal="$AWS_TEST_REPO/.state/$RUN_PREFIX-$lab_role.json"
    lab_instance=$(jq -er '.InstanceId' "$lab_journal") || return 1
    aws --region "$REGION" ec2 wait instance-running --instance-ids "$lab_instance" || return 1
    aws_test_verify "$lab_role" || return 1
    lab_public="${RHEL_HOST#*@}"
    lab_private=$(aws --region "$REGION" ec2 describe-instances --instance-ids "$lab_instance" \
      --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text) || return 1
    python3 -c 'import ipaddress,sys; [ipaddress.IPv4Address(v) for v in sys.argv[1:]]' "$lab_public" "$lab_private" || return 1
    case "$lab_role" in
      gateway) lab_prefix=GATEWAY ;;
      keycloak) lab_prefix=KEYCLOAK ;;
      observability) lab_prefix=OBSERVABILITY ;;
    esac
    export "${lab_prefix}_RHEL_HOST=$RHEL_HOST" "${lab_prefix}_HOST_IP=$lab_public" "${lab_prefix}_PRIVATE_IP=$lab_private"
    printf 'export %s_RHEL_HOST=%q\nexport %s_HOST_IP=%q\nexport %s_PRIVATE_IP=%q\n' \
      "$lab_prefix" "$RHEL_HOST" "$lab_prefix" "$lab_public" "$lab_prefix" "$lab_private"
  done
  printf 'export REPO=%q\nexport SSH_KEY=%q\nexport RUN_PREFIX=%q\nexport REGION=%q\n' "$AWS_TEST_REPO" "$SSH_KEY" "$RUN_PREFIX" "$REGION"
}
