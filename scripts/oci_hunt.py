#!/usr/bin/env python3
"""Safe Oracle A1 Always Free launcher using the OCI Python SDK (not CLI JSON).

Run once per GitHub Actions invocation. No credentials or SSH private key in logs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

import oci


class StopRun(Exception):
    pass


def env(name: str, default: str = "", required: bool = False) -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        raise StopRun(f"Missing GitHub secret/environment: {name}")
    return value


def info(kind: str, message: str) -> None:
    print(f"[{kind}] {message}", flush=True)


def notify(message: str) -> None:
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(request, timeout=12) as reply:
            if reply.status != 200:
                info("WARN", "Telegram notification failed")
    except Exception:
        info("WARN", "Telegram notification unavailable (details hidden)")


def disable_workflow() -> None:
    repo, token = env("GITHUB_REPOSITORY"), env("GH_TOKEN")
    if not repo or not token:
        info("WARN", "Could not auto-disable workflow (missing GitHub token)")
        return
    try:
        result = subprocess.run(
            ["gh", "workflow", "disable", "oracle_sniper.yml", "--repo", repo],
            env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True,
            timeout=25, check=False,
        )
        info("OK" if result.returncode == 0 else "WARN",
             "Workflow disabled" if result.returncode == 0 else "Workflow could not auto-disable; disable manually")
    except (OSError, subprocess.TimeoutExpired):
        info("WARN", "GitHub CLI unavailable; disable workflow manually")


def classify(exc: Exception) -> tuple[str, int | None, str]:
    """Return safe OCI error metadata; never dump keys/headers/raw payloads."""
    if isinstance(exc, oci.exceptions.ServiceError):
        return exc.code or "ServiceError", exc.status, str(exc.message or "OCI API error")[:500]
    return type(exc).__name__, None, str(exc)[:240]


def validate_pubkey() -> str:
    key = env("OCI_SSH_PUBLIC_KEY", required=True)
    parts = key.split()
    if len(parts) < 2 or parts[0] not in {"ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256"}:
        raise StopRun("OCI_SSH_PUBLIC_KEY is not a valid one-line OpenSSH public key")
    if "\n" in key or len(parts[1]) < 80:
        raise StopRun("OCI_SSH_PUBLIC_KEY is empty/invalid")
    return key


def get_clients(config: dict[str, Any]):
    # No automatic retry storms when Oracle reports 429 or capacity errors.
    opts = {"retry_strategy": oci.retry.NoneRetryStrategy()}
    return (
        oci.core.VirtualNetworkClient(config, **opts),
        oci.core.ComputeClient(config, **opts),
        oci.identity.IdentityClient(config, **opts),
        oci.limits.LimitsClient(config, **opts),
    )


def choose_ad(identity: Any, tenancy: str, subnet: Any) -> str:
    given = env("OCI_AVAILABILITY_DOMAIN")
    subnet_ad = getattr(subnet, "availability_domain", None)
    if given and subnet_ad and given != subnet_ad:
        raise StopRun("OCI_AVAILABILITY_DOMAIN conflicts with the subnet's availability domain")
    if given or subnet_ad:
        return given or subnet_ad
    ads = identity.list_availability_domains(tenancy).data
    if not ads:
        raise StopRun("Oracle returned no Availability Domain")
    return ads[0].name


def choose_image(compute: Any, compartment: str) -> str:
    image_id = env("OCI_IMAGE_OCID")
    if image_id:
        if not image_id.startswith("ocid1.image."):
            raise StopRun("OCI_IMAGE_OCID must start with ocid1.image.")
        return image_id
    items = compute.list_images(
        compartment, operating_system="Canonical Ubuntu", shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED", sort_order="DESC", limit=10,
    ).data
    if not items:
        raise StopRun("No compatible Ubuntu ARM image found; set OCI_IMAGE_OCID")
    info("OK", "Selected latest compatible Canonical Ubuntu A1 image")
    return items[0].id


# Note: the exact limit names/availability depend on the tenancy. 404 can mean
# that usage/availability API is unsupported for that limit, not that capacity is free.
LIMITS = (
    ("standard-a1-core-count", "ad", "cpu"),
    ("standard-a1-memory-count", "ad", "memory"),
    ("standard-a1-core-regional-count", "region", "cpu"),
    ("standard-a1-memory-regional-count", "region", "memory"),
)


def check_limits(client: Any, compartment: str, ad: str, ocpus: float, memory: float) -> None:
    for name, scope, unit in LIMITS:
        kwargs = {"availability_domain": ad} if scope == "ad" else {}
        try:
            result = client.get_resource_availability(
                "compute", name, compartment, **kwargs,
            ).data
        except oci.exceptions.ServiceError as exc:
            if exc.status in {404} or (exc.status == 400 and exc.code in {"InvalidParameter", "NotSupported"}):
                info("WARN", f"{name}: usage API unavailable ({exc.code}), quota UNKNOWN")
                continue
            raise
        # Fractional availability is more accurate than rounded availability.
        available = getattr(result, "fractional_available", None)
        if available is None:
            available = getattr(result, "available", None)
        used = getattr(result, "fractional_used", None)
        if used is None:
            used = getattr(result, "used", None)
        info("LIMIT", f"{name}: available={available if available is not None else 'UNKNOWN'}, used={used if used is not None else 'UNKNOWN'}")
        need = ocpus if unit == "cpu" else memory
        if available is not None and float(available) < need:
            raise StopRun(f"LimitExceeded: {name} available={available} < requested={need:g}; do not retry without changing quota")


def get_existing(compute: Any, compartment: str, name: str):
    info("PHASE", "Checking existing instances via OCI Python SDK")
    response = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment, display_name=name,
    )
    for item in response.data:
        if item.display_name == name and item.lifecycle_state not in {"TERMINATED", "TERMINATING"}:
            return item
    return None


def wait_running(compute: Any, instance_id: str) -> str:
    for index in range(15):
        state = compute.get_instance(instance_id).data.lifecycle_state
        info("VM", f"Current state: {state}")
        if state in {"RUNNING", "TERMINATED", "TERMINATING"}:
            return state
        if index < 14:
            time.sleep(15)
    return "PROVISIONING"


def handle_oci_error(exc: Exception, mode: str) -> int:
    code, status, message = classify(exc)
    info("OCI-ERROR", f"code={code}, http={status or 'unknown'}, message={message}")
    if code in {"OutOfHostCapacity", "OutOfCapacity", "OutOfHostCapacityError"} or "out of host capacity" in message.lower():
        info("RETRY-LATER", "Oracle lacks host capacity; next scheduled run can retry")
        return 0
    if status == 429 or code in {"TooManyRequests", "Throttling"}:
        info("BACKOFF", "Oracle throttled the request; wait until next scheduled run")
        return 0
    if code in {"LimitExceeded", "QuotaExceeded"}:
        info("HALT", "Oracle service limit exceeded; scheduling stopped to avoid futile retries")
        notify("Oracle A1 hunt: quota LimitExceeded; check Limits in Oracle Console")
        if mode == "launch":
            disable_workflow()
        return 2
    return 1


def main() -> int:
    tenancy = env("OCI_TENANCY_OCID", required=True)
    region = env("OCI_REGION", required=True)
    subnet_id = env("OCI_SUBNET_ID", required=True)
    compartment = env("OCI_COMPARTMENT_OCID", tenancy)
    mode = env("HUNT_MODE", "check").lower()
    if mode not in {"check", "launch"}:
        raise StopRun("HUNT_MODE must be 'check' or 'launch'")
    cpu = float(env("A1_OCPUS", "2"))
    memory = float(env("A1_MEMORY_GB", "12"))
    if cpu <= 0 or memory <= 0 or cpu > 2 or memory > 12:
        raise StopRun("Safety limit: Always Free A1 maximum 2 OCPU / 12 GB, shared across all A1 instances")
    name = env("A1_DISPLAY_NAME", "Oracle-ARM-Always-Free")
    ssh_key = validate_pubkey() if mode == "launch" else ""
    info("INFO", f"Mode={mode}; region={region}; A1={cpu:g} OCPU/{memory:g}GB")

    config_file = env("OCI_CONFIG_FILE", required=True)
    if not os.path.isfile(config_file):
        raise StopRun("OCI_CONFIG_FILE does not exist")
    info("PHASE", "Loading OCI config and creating SDK clients")
    config = oci.config.from_file(config_file)
    oci.config.validate_config(config)
    network, compute, identity, limits = get_clients(config)

    info("PHASE", "Validating subnet, AD and image")
    subnet = network.get_subnet(subnet_id).data
    if subnet.lifecycle_state != "AVAILABLE":
        raise StopRun(f"Subnet lifecycle_state={subnet.lifecycle_state}; expected AVAILABLE")
    if subnet.prohibit_public_ip_on_vnic:
        raise StopRun("Selected subnet does not allow a public IP")
    ad = choose_ad(identity, tenancy, subnet)
    image_id = choose_image(compute, compartment)
    image = compute.get_image(image_id).data
    if image.lifecycle_state in {"DISABLED", "DELETED"}:
        raise StopRun("Selected OS image has been disabled/deleted")
    info("OK", f"Subnet and image validated; AD={ad}")

    existing = get_existing(compute, compartment, name)
    if existing:
        info("SAFE", f"Existing instance with target name found, state={existing.lifecycle_state}; no new VM will be created")
        if existing.lifecycle_state == "RUNNING" and mode == "launch":
            notify("Oracle A1 already RUNNING; workflow disabled to prevent duplicate launch")
            disable_workflow()
        return 0

    info("PHASE", "Checking limits (unknown is NOT the same as enough quota)")
    try:
        check_limits(limits, compartment, ad, cpu, memory)
    except StopRun as stop:
        info("HALT", str(stop))
        if mode == "launch":
            notify("Oracle A1 hunt stopped: insufficient service quota")
            disable_workflow()
        return 2
    info("OK", "Preflight finished; the limits API may still have UNKNOWN values")
    if mode == "check":
        info("SAFE", "CHECK PASS: no instance created; run workflow with launch to attempt one")
        return 0

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=compartment,
        display_name=name,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=cpu, memory_in_gbs=memory,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=50,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )
    info("TRY", "Sending exactly one launch request")
    try:
        instance = compute.launch_instance(
            details, retry_strategy=oci.retry.NoneRetryStrategy()
        ).data
    except oci.exceptions.ServiceError as exc:
        return handle_oci_error(exc, mode)
    if not instance.id:
        raise StopRun("Oracle accepted launch but did not provide instance ID; check Console before retrying")
    info("OK", "Launch accepted; tracking lifecycle state")
    state = wait_running(compute, instance.id)
    if state == "RUNNING":
        info("SUCCESS", "Oracle A1 VM is RUNNING")
        notify(f"Oracle A1 is RUNNING ({region}, {cpu:g} OCPU/{memory:g} GB). Find its IP in Oracle Console. Private SSH key stays in Cloud Shell.")
        disable_workflow()
        return 0
    if state in {"TERMINATED", "TERMINATING"}:
        info("FAIL", "Instance terminated during creation; inspect Oracle Console")
        return 3
    info("PENDING", "Launch accepted but not RUNNING yet. The next run will detect the existing VM and NOT create another")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StopRun as exc:
        info("STOP", str(exc))
        sys.exit(1)
    except oci.exceptions.ServiceError as exc:
        sys.exit(handle_oci_error(exc, env("HUNT_MODE", "check")))
    except (ValueError, OSError) as exc:
        info("STOP", f"{type(exc).__name__}: {str(exc)[:300]}")
        sys.exit(1)
