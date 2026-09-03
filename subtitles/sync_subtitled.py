"""Automated parallel synchronization tool for distributed AI subtitling.

Manages bidirectional synchronization of source videos and generated subtitles
between local storage and remote worker VMs using Fabric and WinSCP.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Ensure monorepo root is on sys.path so common utilities are importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_sub.shortcode import generate_full_shortcode  # noqa: E402

from common.models import VIDEO_EXTENSIONS, VMStatus, get_file_stem, load_yaml_config, resolve_video_stem  # noqa: E402
from common.ssh import get_connection, get_vm_free_space_gb, list_remote_files, remove_remote_file  # noqa: E402
from common.transfer import (  # noqa: E402
    cleanup_stale_winscp_logs,
    escape_winscp_path,
    resolve_winscp_path,
    run_winscp_command,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("paramiko").setLevel(logging.WARNING)


def query_vm_model_status(vm: dict[str, Any], model_dirs: dict[str, dict[str, str]]) -> VMStatus:
    """Query queues and available space for all configured models on a VM.

    Args:
        vm: Configuration dictionary for the VM.
        model_dirs: Mapping of model shortcode to remote queue directory paths.

    Returns:
        A VMStatus instance with per-model queue mappings stored in the extra dictionary.
    """
    vm_name = vm["name"]
    free_space_gb = 0
    per_model_info: dict[str, dict[str, list[str]]] = {}

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()
            free_space_gb = get_vm_free_space_gb(conn, vm_name)

            for shortcode, dirs in model_dirs.items():
                queued = list_remote_files(sftp, dirs["queue"])
                staging = list_remote_files(sftp, dirs["staging"])
                finished = list_remote_files(sftp, dirs["finished"])
                staging_in = list_remote_files(sftp, dirs["staging_in"])
                per_model_info[shortcode] = {
                    "queued": queued,
                    "staging": staging,
                    "finished": finished,
                    "staging_in": staging_in,
                }
    except Exception as exc:
        logging.error(f"[{vm_name}] Failed to fetch model queues: {exc}")

    return VMStatus(vm=vm, free_space_gb=free_space_gb, extra=per_model_info)


def download_subtitle_file(
    vm: dict[str, Any],
    remote_path: str,
    local_output_dir: str,
    winscp_path: str,
) -> bool:
    """Download a finished subtitle file and delete the remote copy.

    Args:
        vm: Configuration dictionary for the VM.
        remote_path: Remote path to the finished SRT file.
        local_output_dir: Local destination directory for subtitles.
        winscp_path: Path to the winscp.com console executable.

    Returns:
        True if the file was downloaded and verified, False otherwise.
    """
    vm_name = vm["name"]
    filename = os.path.basename(remote_path)
    local_path = os.path.join(local_output_dir, filename)

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()
            remote_size = sftp.stat(remote_path).st_size

            # 3B: Escape WinSCP mask pattern characters
            esc_remote = escape_winscp_path(remote_path)
            esc_local = escape_winscp_path(os.path.abspath(local_path))
            cmd = f'get "{esc_remote}" "{esc_local}"'
            if not run_winscp_command(vm, cmd, winscp_path):
                return False

            if not os.path.exists(local_path) or os.path.getsize(local_path) != remote_size:
                logging.error(f"[{vm_name}] Subtitle download verification failed for {filename}")
                return False

            logging.info(f"[{vm_name}] Downloaded subtitle: {filename}. Cleaning remote copy...")
            remove_remote_file(sftp, remote_path)
            return True
    except Exception as exc:
        logging.error(f"[{vm_name}] Error downloading subtitle {filename}: {exc}")
        return False


def upload_sub_video(
    vm: dict[str, Any],
    local_path: str,
    staging_in_dir: str,
    queue_dir: str,
    winscp_path: str,
) -> bool:
    """Upload a video to a model's staging area and move it to the transcribing queue.

    Args:
        vm: Configuration dictionary for the VM.
        local_path: Local path of the video file to upload.
        staging_in_dir: Remote staging directory for incoming uploads.
        queue_dir: Remote processing queue directory.
        winscp_path: Path to the winscp.com console executable.

    Returns:
        True if the file was uploaded and enqueued successfully, False otherwise.
    """
    vm_name = vm["name"]
    filename = os.path.basename(local_path)
    remote_staging = f"{staging_in_dir}/{filename}"
    remote_queue = f"{queue_dir}/{filename}"

    logging.info(f"[{vm_name}] Uploading '{filename}' to {staging_in_dir}...")

    try:
        with get_connection(vm) as conn:
            sftp = conn.sftp()
            local_size = os.path.getsize(local_path)

            # 3B: Escape WinSCP mask pattern characters
            esc_local = escape_winscp_path(os.path.abspath(local_path))
            esc_staging = escape_winscp_path(remote_staging)
            cmd = f'put -resume "{esc_local}" "{esc_staging}"'
            if not run_winscp_command(vm, cmd, winscp_path):
                return False

            if sftp.stat(remote_staging).st_size != local_size:
                logging.error(f"[{vm_name}] Size mismatch after upload for {filename}")
                return False

            mv_res = conn.run(f'mv "{remote_staging}" "{remote_queue}"', hide=True, warn=True)
            if mv_res.ok:
                logging.info(f"[{vm_name}] Successfully queued '{filename}' in {queue_dir}.")
                return True

            logging.error(f"[{vm_name}] Failed to enqueue video: {mv_res.stderr}")
            return False
    except Exception as exc:
        logging.error(f"[{vm_name}] Error uploading {filename}: {exc}")
        return False


def main() -> None:
    """Execute the main parallel subtitle synchronization routine.

    Raises:
        ValueError: If configuration keys or VM lists are missing.
    """
    cleanup_stale_winscp_logs()

    config_path = Path(__file__).parent / "config_subtitles.yaml"
    config = load_yaml_config(config_path)

    local_cfg = config.get("local", {})
    local_input_dir = local_cfg.get("input_dir")
    local_output_dir = local_cfg.get("output_dir")
    if not local_input_dir or not local_output_dir:
        raise ValueError("Missing 'local.input_dir' or 'local.output_dir' in configuration.")

    settings_cfg = config.get("settings", {})
    min_space_gb = int(settings_cfg.get("min_vm_free_space_gb", 15))
    max_workers = int(settings_cfg.get("max_concurrent_workers", 8))
    remote_base = settings_cfg.get("remote_base_dir", "/home/ubuntu/media-pipeline/subtitles").rstrip("/")

    models_cfg = config.get("models", [])
    if not models_cfg:
        raise ValueError("No models configured in 'models' list.")

    model_dirs: dict[str, dict[str, str]] = {}
    model_queue_limits: dict[str, int] = {}
    for m in models_cfg:
        shortcode = generate_full_shortcode(m["model_name"])
        base = f"{remote_base}/models/{shortcode}"
        model_dirs[shortcode] = {
            "staging_in": f"{base}/01_upload_staging",
            "queue": f"{base}/02_sub_queue",
            "staging": f"{base}/03_sub_staging",
            "finished": f"{base}/04_sub_finished",
        }
        model_queue_limits[shortcode] = int(m.get("max_queue_per_vm", 1))

    winscp_path = resolve_winscp_path(local_cfg.get("winscp_path"))
    vms = config.get("vms", [])
    if not vms:
        raise ValueError("No VMs configured in 'vms' list.")

    os.makedirs(local_output_dir, exist_ok=True)

    logging.info("--- Phase 1: Parallel VM Status Discovery ---")
    vm_statuses: list[VMStatus] = []
    with ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [executor.submit(query_vm_model_status, vm, model_dirs) for vm in vms]
        for future in as_completed(futures):
            vm_statuses.append(future.result())

    download_tasks: list[tuple[dict[str, Any], str]] = []
    vm_space_map: dict[str, int] = {}
    vm_model_loads: dict[str, dict[str, int]] = {}
    active_remotes_per_model: dict[str, set[str]] = {sc: set() for sc in model_dirs}
    staged_in_per_model: dict[str, dict[str, set[str]]] = {sc: {} for sc in model_dirs}

    for status in vm_statuses:
        ip = status.vm["ip"]
        vm_space_map[ip] = status.free_space_gb
        vm_model_loads[ip] = {}

        for shortcode, queues in status.extra.items():
            # 4A: Count distinct stems being processed to avoid staging artifact overcounting
            active_stems = {resolve_video_stem(f, shortcode) for f in queues["queued"] + queues["staging"]}
            vm_model_loads[ip][shortcode] = len(active_stems)

            # Add active stems already queued, transcoding, or finished
            for f in queues["queued"] + queues["staging"] + queues["finished"]:
                active_remotes_per_model[shortcode].add(resolve_video_stem(f, shortcode))

            # 1C: Track in-flight uploads in staging_in separately for resumption
            staged_in_per_model[shortcode][ip] = {resolve_video_stem(f, shortcode) for f in queues["staging_in"]}

            for f in queues["finished"]:
                if f.endswith(".srt"):
                    remote_srt = f"{model_dirs[shortcode]['finished']}/{f}"
                    download_tasks.append((status.vm, remote_srt))

    logging.info("--- Phase 2: Scanning Local Videos & Planning Uploads ---")
    local_videos = [
        f
        for f in glob.glob(os.path.join(local_input_dir, "*"))
        if os.path.isfile(f) and f.lower().endswith(VIDEO_EXTENSIONS)
    ]

    upload_tasks: list[tuple[dict[str, Any], str, str, str]] = []

    for shortcode, dirs in model_dirs.items():
        max_q = model_queue_limits[shortcode]
        expected_suffix = f".{shortcode}.srt"

        candidates = [
            f
            for f in local_videos
            if not os.path.exists(os.path.join(local_output_dir, f"{get_file_stem(f)}{expected_suffix}"))
            and get_file_stem(f) not in active_remotes_per_model[shortcode]
        ]

        for file_path in candidates:
            stem = get_file_stem(file_path)
            file_size_gb = os.path.getsize(file_path) / (1024**3)
            required_space = max(min_space_gb, math.ceil(file_size_gb * 2.5))

            assigned_vm = None
            resumed = False

            # 1C: Attempt to resume in-flight uploads on the staging VM first
            for s in vm_statuses:
                ip = s.vm["ip"]
                if stem in staged_in_per_model[shortcode].get(ip, set()):
                    current_load = vm_model_loads[ip].get(shortcode, 0)
                    if current_load < max_q and vm_space_map[ip] >= required_space:
                        assigned_vm = s.vm
                        upload_tasks.append((assigned_vm, file_path, dirs["staging_in"], dirs["queue"]))
                        vm_model_loads[ip][shortcode] += 1
                        vm_space_map[ip] -= math.ceil(file_size_gb * 2.5)
                        logging.info(
                            f"Resuming upload of '{os.path.basename(file_path)}' to {assigned_vm['name']} for model "
                            f"{shortcode} "
                            f"(Model queue: {vm_model_loads[ip][shortcode]}/{max_q}, Space left: {vm_space_map[ip]} "
                            f"GB)."
                        )
                    else:
                        logging.warning(
                            f"Cannot resume upload of '{os.path.basename(file_path)}' on {s.vm['name']} "
                            f"for {shortcode}: "
                            f"queue full ({current_load}/{max_q}) or insufficient space ({vm_space_map[ip]} GB"
                            f" / {required_space} GB needed)."
                        )
                    resumed = True
                    break

            if resumed:
                continue

            # Fresh upload assignment: balance across VMs
            sorted_vms = sorted(
                vm_statuses,
                key=lambda s: vm_model_loads[s.vm["ip"]].get(shortcode, 0),
            )

            for s in sorted_vms:
                ip = s.vm["ip"]
                current_load = vm_model_loads[ip].get(shortcode, 0)
                if current_load < max_q and vm_space_map[ip] >= required_space:
                    assigned_vm = s.vm
                    upload_tasks.append((assigned_vm, file_path, dirs["staging_in"], dirs["queue"]))
                    vm_model_loads[ip][shortcode] += 1
                    vm_space_map[ip] -= math.ceil(file_size_gb * 2.5)
                    logging.info(
                        f"Assigned '{os.path.basename(file_path)}' to {assigned_vm['name']} for model {shortcode} "
                        f"(Model queue: {vm_model_loads[ip][shortcode]}/{max_q}, Space left: {vm_space_map[ip]} GB)."
                    )
                    break

            if not assigned_vm:
                logging.debug(
                    f"Queue full or insufficient space across VMs for {os.path.basename(file_path)} on {shortcode}."
                )

    logging.info(f"--- Phase 3: Execution ({len(download_tasks)} downloads, {len(upload_tasks)} uploads) ---")

    if not download_tasks and not upload_tasks:
        logging.info("No active downloads or uploads required. Subtitles are up to date.")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        task_futures = {}

        for vm, remote_srt in download_tasks:
            future = executor.submit(
                download_subtitle_file,
                vm,
                remote_srt,
                local_output_dir,
                winscp_path,
            )
            task_futures[future] = f"Download '{os.path.basename(remote_srt)}' from {vm['name']}"

        for vm, file_path, staging_in, queue in upload_tasks:
            future = executor.submit(
                upload_sub_video,
                vm,
                file_path,
                staging_in,
                queue,
                winscp_path,
            )
            task_futures[future] = f"Upload '{os.path.basename(file_path)}' to {vm['name']} queue"

        for future in as_completed(task_futures):
            task_description = task_futures[future]
            try:
                success = future.result()
                status_label = "SUCCESS" if success else "FAILED"
                logging.info(f"[{status_label}] {task_description}")
            except Exception as exc:
                logging.error(f"[ERROR] {task_description} raised an unexpected exception: {exc}")

    logging.info("--- Parallel subtitle sync completed ---")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Sync aborted by user.")
        sys.exit(0)
