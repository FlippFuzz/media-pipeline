"""Automated parallel synchronization tool for remote video transcoding.

Manages bidirectional synchronization of media files between local storage and
remote transcoding worker VMs using Fabric (SSH/SFTP) and WinSCP.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Ensure monorepo root is on sys.path so common utilities are importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.models import VIDEO_EXTENSIONS, VMStatus, get_file_stem, load_yaml_config  # noqa: E402
from common.ssh import get_connection, get_vm_free_space_gb, list_remote_files  # noqa: E402
from common.transfer import (  # noqa: E402
    cleanup_stale_winscp_logs,
    escape_winscp_path,
    resolve_winscp_path,
    run_winscp_command,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("paramiko").setLevel(logging.WARNING)

SPACE_SAFETY_MULTIPLIER = 3.0


def query_single_vm_status(vm: dict[str, Any], remote_dirs: dict[str, str]) -> VMStatus:
    """Query file queues and available disk space for a single transcoding VM.

    Args:
        vm: Configuration dictionary for the VM.
        remote_dirs: Mapping of queue names to remote directory paths.

    Returns:
        A VMStatus instance populated with current remote queue state.
    """
    vm_name = vm["name"]
    queued_files: list[str] = []
    staging_out_files: list[str] = []
    completed_files: list[str] = []
    staging_in_files: list[str] = []
    free_space_gb = 0

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()
            queued_files = list_remote_files(sftp, remote_dirs["input"])
            staging_out_files = list_remote_files(sftp, remote_dirs["staging_out"])
            completed_files = list_remote_files(sftp, remote_dirs["output"])
            staging_in_files = list_remote_files(sftp, remote_dirs["staging_in"])
            free_space_gb = get_vm_free_space_gb(conn, vm_name)

        logging.info(
            f"[{vm_name}] Status: {len(queued_files)} queued, {len(staging_out_files)} transcoding, "
            f"{len(completed_files)} finished, {len(staging_in_files)} staging/partial, {free_space_gb} GB free."
        )
    except Exception as exc:
        logging.error(f"[{vm_name}] Failed to fetch remote status: {exc}")

    return VMStatus(
        vm=vm,
        free_space_gb=free_space_gb,
        queued_files=queued_files,
        staging_out_files=staging_out_files,
        completed_files=completed_files,
        staging_in_files=staging_in_files,
    )


def download_completed_file(
    vm: dict[str, Any],
    filename: str,
    local_input_dir: str,
    local_output_dir: str,
    winscp_path: str,
    remote_dirs: dict[str, str],
) -> bool:
    """Download a completed video from a remote VM or relocate local original if unoptimized.

    Args:
        vm: Configuration dictionary for the VM.
        filename: Name of the finished file on the remote host.
        local_input_dir: Local path containing raw original videos.
        local_output_dir: Local destination path for finished videos.
        winscp_path: Absolute path to the winscp.com executable.
        remote_dirs: Mapping of remote directory names to paths.

    Returns:
        True if the file was downloaded or relocated successfully, False otherwise.
    """
    vm_name = vm["name"]
    remote_path = f"{remote_dirs['output']}/{filename}"
    local_path = os.path.join(local_output_dir, filename)

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()
            remote_size = sftp.stat(remote_path).st_size

            base_stem = get_file_stem(filename)
            local_original: str | None = None
            for ext in VIDEO_EXTENSIONS:
                candidate = os.path.join(local_input_dir, base_stem + ext)
                if os.path.exists(candidate):
                    local_original = candidate
                    break

            if local_original and remote_size >= os.path.getsize(local_original):
                logging.info(
                    f"[{vm_name}] '{filename}' not smaller than original. Relocating local original to output..."
                )
                shutil.move(local_original, local_path)
                sftp.remove(remote_path)
                return True

            local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if local_size > remote_size:
                logging.warning(f"[{vm_name}] Local file larger than remote. Overwriting '{filename}'.")
                os.remove(local_path)
                local_size = 0

            if local_size < remote_size:
                logging.info(f"[{vm_name}] Downloading '{filename}'...")
                # 3B: Escape WinSCP mask pattern characters
                esc_remote = escape_winscp_path(remote_path)
                esc_local = escape_winscp_path(os.path.abspath(local_path))
                cmd = f'get -resume "{esc_remote}" "{esc_local}"'
                if not run_winscp_command(vm, cmd, winscp_path):
                    return False

            actual_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if actual_size != remote_size:
                logging.error(f"[{vm_name}] Size mismatch for '{filename}': Remote={remote_size}, Local={actual_size}")
                return False

            logging.info(f"[{vm_name}] Download verified: '{filename}'. Cleaning up remote copy...")
            sftp.remove(remote_path)

            if local_original and os.path.exists(local_original):
                logging.info(f"[{vm_name}] Deleting original raw file: {local_original}")
                os.remove(local_original)

            return True
    except Exception as exc:
        logging.error(f"[{vm_name}] Error downloading '{filename}': {exc}")
        return False


def upload_input_file(
    vm: dict[str, Any],
    local_path: str,
    winscp_path: str,
    remote_dirs: dict[str, str],
) -> bool:
    """Upload a raw video file to staging and atomically move it to the transcode queue.

    Args:
        vm: Configuration dictionary for the VM.
        local_path: Local path of the video file to upload.
        winscp_path: Absolute path to the winscp.com executable.
        remote_dirs: Mapping of remote directory names to paths.

    Returns:
        True if the file was uploaded and enqueued successfully, False otherwise.
    """
    vm_name = vm["name"]
    filename = os.path.basename(local_path)
    remote_staging_path = f"{remote_dirs['staging_in']}/{filename}"
    remote_filepart_path = f"{remote_staging_path}.filepart"
    remote_input_path = f"{remote_dirs['input']}/{filename}"

    logging.info(f"[{vm_name}] Starting upload for '{filename}'...")

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()

            remote_size = 0
            partial_target = remote_staging_path
            try:
                remote_size = sftp.stat(remote_staging_path).st_size
            except OSError:
                try:
                    remote_size = sftp.stat(remote_filepart_path).st_size
                    partial_target = remote_filepart_path
                except OSError:
                    pass

            local_size = os.path.getsize(local_path)
            if remote_size > local_size:
                logging.warning(f"[{vm_name}] Staging file larger than local. Overwriting '{filename}'.")
                try:
                    sftp.remove(partial_target)
                except OSError:
                    pass
                remote_size = 0

            if remote_size < local_size:
                # 3B: Escape WinSCP mask pattern characters
                esc_local = escape_winscp_path(os.path.abspath(local_path))
                esc_staging = escape_winscp_path(remote_staging_path)
                cmd = f'put -resume "{esc_local}" "{esc_staging}"'
                if not run_winscp_command(vm, cmd, winscp_path):
                    return False

            remote_stat = sftp.stat(remote_staging_path)
            if remote_stat.st_size != local_size:
                logging.error(
                    f"[{vm_name}] Size mismatch for '{filename}': Local={local_size}, Remote={remote_stat.st_size}"
                )
                return False

            logging.info(f"[{vm_name}] Upload verified. Moving '{filename}' to transcode queue...")
            mv_res = conn.run(f'mv "{remote_staging_path}" "{remote_input_path}"', hide=True, warn=True)
            if mv_res.ok:
                logging.info(f"[{vm_name}] Successfully enqueued '{filename}'.")
                return True

            logging.error(f"[{vm_name}] Failed to enqueue file: {mv_res.stderr}")
            return False
    except Exception as exc:
        logging.error(f"[{vm_name}] Error uploading '{filename}': {exc}")
        return False


def main() -> None:
    """Execute the main parallel transcoding synchronization routine.

    Raises:
        ValueError: If mandatory local directory paths or VM configurations are missing.
    """
    cleanup_stale_winscp_logs()

    config_path = Path(__file__).parent / "config_transcode.yaml"
    config = load_yaml_config(config_path)

    local_cfg = config.get("local", {})
    local_input_dir = local_cfg.get("input_dir")
    local_output_dir = local_cfg.get("output_dir")
    if not local_input_dir or not local_output_dir:
        raise ValueError("Missing 'local.input_dir' or 'local.output_dir' in configuration.")

    settings_cfg = config.get("settings", {})
    min_space_gb = int(settings_cfg.get("min_vm_free_space_gb", 15))
    max_workers = int(settings_cfg.get("max_concurrent_workers", 8))
    remote_base = settings_cfg.get("remote_base_dir", "/home/ubuntu/media-pipeline/transcode").rstrip("/")

    remote_dirs = {
        "staging_in": f"{remote_base}/01_upload_staging",
        "input": f"{remote_base}/02_transcode_queue",
        "staging_out": f"{remote_base}/03_transcode_staging",
        "output": f"{remote_base}/04_transcode_finished",
    }

    winscp_path = resolve_winscp_path(local_cfg.get("winscp_path"))
    vms = config.get("vms", [])
    if not vms:
        raise ValueError("No VMs configured in 'vms' list.")

    vm_order_map = {vm["ip"]: idx for idx, vm in enumerate(vms)}
    os.makedirs(local_output_dir, exist_ok=True)

    logging.info("--- Phase 1: Parallel VM Status Discovery ---")
    vm_statuses: list[VMStatus] = []
    with ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [executor.submit(query_single_vm_status, vm, remote_dirs) for vm in vms]
        for future in as_completed(futures):
            vm_statuses.append(future.result())

    vm_statuses.sort(key=lambda s: vm_order_map[s.vm["ip"]])

    active_remotes: set[str] = set()
    staged_map: dict[str, set[str]] = {}
    vm_loads: dict[str, int] = {}
    download_tasks: list[tuple[dict[str, Any], str]] = []
    vm_space_map: dict[str, int] = {}

    for status in vm_statuses:
        ip = status.vm["ip"]
        staged_map[ip] = set()
        vm_loads[ip] = len(status.queued_files) + len(status.staging_out_files)
        vm_space_map[ip] = status.free_space_gb

        for f in status.queued_files + status.staging_out_files + status.completed_files:
            active_remotes.add(get_file_stem(f))

        for f in status.staging_in_files:
            staged_map[ip].add(get_file_stem(f))

        for f in status.completed_files:
            download_tasks.append((status.vm, f))

    logging.info("--- Phase 2: Scanning Local Files & Balancing Uploads ---")
    local_candidates = [
        f
        for f in glob.glob(os.path.join(local_input_dir, "*"))
        if os.path.isfile(f) and f.lower().endswith(VIDEO_EXTENSIONS)
    ]

    # 4C: Exclude local videos that already have finished transcodes in local_output_dir
    files_to_upload = [
        f
        for f in local_candidates
        if get_file_stem(os.path.basename(f)) not in active_remotes
        and not any(
            os.path.exists(os.path.join(local_output_dir, f"{get_file_stem(os.path.basename(f))}{ext}"))
            for ext in VIDEO_EXTENSIONS
        )
    ]

    upload_tasks: list[tuple[dict[str, Any], str]] = []
    unassigned_files: list[str] = []

    # 4B: Space checking and deduction when resuming in-flight uploads
    for file_path in files_to_upload:
        stem = get_file_stem(os.path.basename(file_path))
        file_size_gb = os.path.getsize(file_path) / (1024**3)
        required_space = max(min_space_gb, math.ceil(file_size_gb * SPACE_SAFETY_MULTIPLIER))
        resumed = False

        for status in vm_statuses:
            vm_ip = status.vm["ip"]
            if stem in staged_map[vm_ip]:
                if vm_space_map[vm_ip] >= required_space:
                    upload_tasks.append((status.vm, file_path))
                    vm_loads[vm_ip] += 1
                    vm_space_map[vm_ip] -= math.ceil(file_size_gb * SPACE_SAFETY_MULTIPLIER)
                    resumed = True
                    logging.info(
                        f"Resuming upload of '{os.path.basename(file_path)}' on {status.vm['name']} "
                        f"(Load: {vm_loads[vm_ip]}, Est. Space Left: {vm_space_map[vm_ip]} GB)."
                    )
                else:
                    logging.warning(
                        f"Cannot resume upload of '{os.path.basename(file_path)}' on {status.vm['name']}: "
                        f"insufficient space ({vm_space_map[vm_ip]} GB available, {required_space} GB needed)."
                    )
                    resumed = True
                break

        if not resumed and not any(stem in staged_map[s.vm["ip"]] for s in vm_statuses):
            unassigned_files.append(file_path)

    for file_path in unassigned_files:
        file_size_gb = os.path.getsize(file_path) / (1024**3)
        required_space = max(min_space_gb, math.ceil(file_size_gb * SPACE_SAFETY_MULTIPLIER))

        sorted_vms = sorted(
            vm_statuses,
            key=lambda s: (vm_loads[s.vm["ip"]], vm_order_map[s.vm["ip"]]),
        )
        assigned_vm = None

        for status in sorted_vms:
            ip = status.vm["ip"]
            if vm_space_map[ip] >= required_space:
                assigned_vm = status.vm
                upload_tasks.append((assigned_vm, file_path))
                vm_loads[ip] += 1
                vm_space_map[ip] -= math.ceil(file_size_gb * SPACE_SAFETY_MULTIPLIER)
                logging.info(
                    f"Assigned '{os.path.basename(file_path)}' to {assigned_vm['name']} "
                    f"(Load: {vm_loads[ip]}, Est. Space Left: {vm_space_map[ip]} GB)."
                )
                break

        if not assigned_vm:
            logging.warning(
                f"Skipping '{os.path.basename(file_path)}' - No VM has sufficient space ({required_space} GB needed)."
            )

    logging.info(f"--- Phase 3: Parallel Execution ({len(download_tasks)} downloads, {len(upload_tasks)} uploads) ---")

    if not download_tasks and not upload_tasks:
        logging.info("No active downloads or uploads required. Everything is up to date.")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        task_futures = {}

        for vm, filename in download_tasks:
            future = executor.submit(
                download_completed_file,
                vm,
                filename,
                local_input_dir,
                local_output_dir,
                winscp_path,
                remote_dirs,
            )
            task_futures[future] = f"Download '{filename}' from {vm['name']}"

        for vm, file_path in upload_tasks:
            future = executor.submit(
                upload_input_file,
                vm,
                file_path,
                winscp_path,
                remote_dirs,
            )
            task_futures[future] = f"Upload '{os.path.basename(file_path)}' to {vm['name']}"

        for future in as_completed(task_futures):
            task_description = task_futures[future]
            try:
                success = future.result()
                status_label = "SUCCESS" if success else "FAILED"
                logging.info(f"[{status_label}] {task_description}")
            except Exception as exc:
                logging.error(f"[ERROR] {task_description} raised an unexpected exception: {exc}")

    logging.info("--- Parallel transcode sync completed ---")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Sync aborted by user.")
        sys.exit(0)
