#!/usr/bin/env python3
"""OCI Always Free A1 2 OCPU/12GB safe single-attempt launcher.

No private SSH keys are generated/uploaded; no repeated fast polling of launch.
Designed for a fresh GitHub Actions runner invoked every 30 minutes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class OCIError(Exception):
    code: str
    message: str
    status: int | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required GitHub secret / environment variable: {name}")
    return value


def safe_num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def error_from_text(message: str) -> OCIError:
    code = re.search(r'"code"\s*:\s*"([^"]+)"', message)
    status = re.search(r'"status"\s*:\s*(\d+)', message)
    reason = re.search(r'"message"\s*:\s*"([^"]+)"', message)
    if reason:
        detail = reason.group(1)
    else:
        detail = message.strip().splitlines()[-1][:350] if message.strip() else "No OCI error details returned"
    return OCIError(code.group(1) if code else "OCIError", detail, int(status.group(1)) if status else None)


def oci(*args: str, timeout: int = 85) -> dict[str, Any]:
    config = required("OCI_CONFIG_FILE")
    cmd = ["oci", *args, "--config-file", config, "--no-retry", "--output", "json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise OCIError("Timeout", f"OCI request timed out after {timeout}s") from None
    if p.returncode != 0:
        raise error_from_text(p.stderr or p.stdout)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        raise OCIError("BadJSON", "OCI CLI returned invalid JSON") from None


def data(*args: str, timeout: int = 85) -> Any:
    return oci(*args, timeout=timeout).get("data")


def notify(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        print("[INFO] Telegram not configured; no message sent")
        return
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status != 200:
                print("[WARN] Telegram notification returned a non-200 status")
    except Exception:
        print("[WARN] Cannot send Telegram message (token/details hidden)")


def disable_workflow() -> bool:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    token = os.getenv("GH_TOKEN", "")
    if not repo or not token:
        print("[WARN] Cannot disable workflow automatically (missing GitHub context)")
        return False
    command = ["gh", "workflow", "disable", "oracle_sniper.yml", "--repo", repo]
    try:
        result = subprocess.run(command, env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            print("[OK] Scheduled workflow disabled; no more retries")
            return True
        print("[WARN] Unable to disable workflow; check repo Actions permissions")
    except (OSError, subprocess.TimeoutExpired):
        print("[WARN] GitHub CLI unavailable / timed out while disabling workflow")
    return False


def get_ad(tenancy: str, subnet: dict[str, Any]) -> str:
    ad_override = os.getenv("OCI_AVAILABILITY_DOMAIN", "").strip()
    subnet_ad = (subnet.get("availability-domain") or "").strip()
    if ad_override and subnet_ad and ad_override != subnet_ad:
        raise ValueError("OCI_AVAILABILITY_DOMAIN conflicts with subnet availability domain")
    if ad_override or subnet_ad:
        return ad_override or subnet_ad
    ads = data("iam", "availability-domain", "list", "--compartment-id", tenancy)
    if not isinstance(ads, list) or not ads:
        raise ValueError("No availability domain returned by OCI")
    return ads[0]["name"]


def resolve_image(compartment: str) -> str:
    explicit = os.getenv("OCI_IMAGE_OCID", "").strip()
    if explicit:
        if not explicit.startswith("ocid1.image."):
            raise ValueError("OCI_IMAGE_OCID must start with ocid1.image.")
        return explicit
    images = data("compute", "image", "list", "--compartment-id", compartment,
                  "--operating-system", "Canonical Ubuntu", "--shape", "VM.Standard.A1.Flex",
                  "--sort-by", "TIMECREATED", "--sort-order", "DESC", "--limit", "10")
    if not images:
        raise ValueError("No Ubuntu ARM-compatible image returned. Set OCI_IMAGE_OCID explicitly")
    selected = images[0]["id"]
    print("[INFO] Using most recent Ubuntu A1-compatible platform image from OCI")
    return selected


LIMITS = [
    ("standard-a1-core-count", "ad", "cpu"),
    ("standard-a1-memory-count", "ad", "ram"),
    ("standard-a1-core-regional-count", "regional", "cpu"),
    ("standard-a1-memory-regional-count", "regional", "ram"),
]


def check_limits(compartment: str, ad: str, ocpus: float, memory: float) -> None:
    """Stop only if OCI affirmatively returns insufficient quota.

    Some OCI tenancy/limits do not implement this API: unknown is reported, not
    misrepresented as capacity or as a confirmed limit.
    """
    for limit, scope, unit in LIMITS:
        cmd = ["limits", "resource-availability", "get",
               "--service-name", "compute", "--limit-name", limit, "--compartment-id", compartment]
        if scope == "ad":
            cmd += ["--availability-domain", ad]
        try:
            result = data(*cmd, timeout=35)
        except OCIError as e:
            if e.code in {"NotAuthenticated", "NotAuthorizedOrNotFound"} and e.status == 401:
                raise
            print(f"[WARN] Limit {limit}: not accessible ({e.code}); capacity remains UNKNOWN")
            continue
        if not isinstance(result, dict):
            print(f"[WARN] Limit {limit}: no resource availability returned")
            continue
        free = safe_num(result.get("fractional-available"))
        if free is None:
            free = safe_num(result.get("available"))
        used = result.get("used", "unknown")
        print(f"[LIMIT] {limit}: available={free if free is not None else 'unknown'}, used={used}")
        required_qty = ocpus if unit == "cpu" else memory
        if free is not None and free < required_qty:
            raise OCIError("LimitExceeded", f"Not enough quota for {limit}: requires {required_qty}; available {free}")


def get_existing(compartment: str, name: str) -> dict[str, Any] | None:
    instances = data("compute", "instance", "list", "--compartment-id", compartment, "--all")
    for inst in instances or []:
        if inst.get("display-name") == name and inst.get("lifecycle-state") not in {"TERMINATED", "TERMINATING"}:
            return inst
    return None


def wait_running(instance_id: str) -> str:
    for attempt in range(20):
        inst = data("compute", "instance", "get", "--instance-id", instance_id, timeout=40)
        state = inst.get("lifecycle-state", "UNKNOWN")
        print(f"[VM] instance status: {state}")
        if state == "RUNNING":
            return state
        if state in {"TERMINATED", "TERMINATING"}:
            return state
        if attempt < 19:
            time.sleep(15)
    return "PROVISIONING_OR_UNKNOWN"


def validate_ssh_pubkey() -> str:
    key = required("OCI_SSH_PUBLIC_KEY")
    if "\n" in key or not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise ValueError("OCI_SSH_PUBLIC_KEY must be a single OpenSSH public key")
    return key


def main() -> int:
    tenancy = required("OCI_TENANCY_OCID")
    region = required("OCI_REGION")
    compartment = os.getenv("OCI_COMPARTMENT_OCID", "").strip() or tenancy
    subnet_id = required("OCI_SUBNET_ID")
    name = os.getenv("A1_DISPLAY_NAME", "Oracle-ARM-Always-Free")
    ocpus = float(os.getenv("A1_OCPUS", "2"))
    memory = float(os.getenv("A1_MEMORY_GB", "12"))
    mode = os.getenv("HUNT_MODE", "check").strip().lower()

    # Refuse requests beyond the documented 2026 Always Free A1 allowance.
    if ocpus <= 0 or memory <= 0 or ocpus > 2 or memory > 12:
        raise ValueError("STOP: 2026 Always Free A1 max total = 2 OCPUs / 12 GB RAM")
    if mode not in {"check", "launch"}:
        raise ValueError("HUNT_MODE must be check or launch")
    print(f"[INFO] Mode={mode}, region={region}, shape=VM.Standard.A1.Flex, {ocpus:g} OCPU/{memory:g} GB")

    subnet = data("network", "subnet", "get", "--subnet-id", subnet_id)
    if subnet.get("prohibit-public-ip-on-vnic") is True:
        raise ValueError("Subnet does not allow public IP; choose the existing public subnet")
    if subnet.get("lifecycle-state") not in {"AVAILABLE", None}:
        raise ValueError("Subnet must be AVAILABLE")
    ad = get_ad(tenancy, subnet)
    image_id = resolve_image(compartment)
    # Verify image is known to OCI before launching.
    image = data("compute", "image", "get", "--image-id", image_id)
    if image.get("lifecycle-state") in {"DISABLED", "DELETED"}:
        raise ValueError("Selected image is disabled/deleted; choose a current Ubuntu ARM image")
    print(f"[OK] Network and image validated; AD={ad}")

    existing = get_existing(compartment, name)
    if existing:
        state = existing.get("lifecycle-state", "UNKNOWN")
        print(f"[INFO] Existing named instance detected: status={state}; no duplicate will be created")
        if mode == "launch" and state == "RUNNING":
            notify(f"Oracle A1 2 OCPU/12GB already exists and is RUNNING in {region}. Check OCI Console.")
            disable_workflow()
        return 0

    try:
        check_limits(compartment, ad, ocpus, memory)
    except OCIError as err:
        if err.code == "LimitExceeded" and mode == "launch":
            print(f"[HALT] {err.message}; no VM launch attempted")
            notify("Oracle A1 hunt stopped: available A1 quota is insufficient. Check OCI Limits.")
            disable_workflow()
        raise
    print("[OK] Preflight complete (limits without API data remain UNKNOWN, not guaranteed)")
    if mode == "check":
        print("[SAFE] Check mode: no VM was created. Use Run workflow > launch to attempt creation.")
        return 0

    ssh_key = validate_ssh_pubkey()
    import tempfile
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="a1-key-", delete=True) as keyfile:
        keyfile.write(ssh_key + "\n")
        keyfile.flush()
        os.chmod(keyfile.name, 0o600)
        print("[TRY] Sending ONE VM launch request; not retrying rapidly")
        try:
            inst = data("compute", "instance", "launch",
                        "--availability-domain", ad,
                        "--compartment-id", compartment,
                        "--shape", "VM.Standard.A1.Flex",
                        "--shape-config", json.dumps({"ocpus": ocpus, "memoryInGBs": memory}),
                        "--image-id", image_id,
                        "--subnet-id", subnet_id,
                        "--assign-public-ip", "true",
                        "--display-name", name,
                        "--ssh-authorized-keys-file", keyfile.name,
                        "--boot-volume-size-in-gbs", "50", timeout=95)
        except OCIError as err:
            print(f"[OCI] {err.code}: {err.message}")
            if err.code in {"OutOfHostCapacity", "OutOfCapacity", "OutOfHostCapacityError"} or "out of host capacity" in err.message.lower():
                print("[RETRY-LATER] No capacity. Next scheduled attempt in ~30 min. Nothing created.")
                return 0
            if err.code in {"TooManyRequests", "Throttling"} or err.status == 429:
                print("[BACKOFF] OCI rate limit. No immediate retry; next schedule will try again.")
                return 0
            if err.code in {"LimitExceeded", "QuotaExceeded"}:
                notify("Oracle A1 hunt stopped: account lacks free A1 quota. See Actions log / OCI Limits.")
                disable_workflow()
                return 2
            raise

    instance_id = inst.get("id")
    if not instance_id:
        raise OCIError("MissingInstanceId", "OCI response did not contain instance id; inspect OCI Console before retrying")
    print("[OK] Oracle accepted launch request; checking VM lifecycle")
    status = wait_running(instance_id)
    if status == "RUNNING":
        print("[SUCCESS] A1 VM is RUNNING; no more launch requests")
        notify(f"Success! Oracle A1 {ocpus:g} OCPU/{memory:g} GB is RUNNING in {region}. Open OCI Console to find its IP. SSH key remains with you; nothing private was sent to Telegram.")
        disable_workflow()
        return 0
    if status in {"TERMINATED", "TERMINATING"}:
        print("[FAIL] Instance was terminated during provisioning; inspect OCI Console")
        return 3
    print("[PENDING] Launch accepted; VM not confirmed RUNNING yet. No new launch until next scheduled check")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OCIError) as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        sys.exit(1)
