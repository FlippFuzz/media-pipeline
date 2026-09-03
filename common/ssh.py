"""SSH and SFTP connection management using Fabric and Paramiko."""

from __future__ import annotations

import logging
import os
from typing import Any

from fabric import Config, Connection
from paramiko import AutoAddPolicy

logger = logging.getLogger(__name__)


def get_connection(vm: dict[str, Any], timeout: int = 10) -> Connection:
    """Establish a configured Fabric SSH connection to a remote worker VM.

    Args:
        vm: Configuration dictionary containing connection parameters ('ip', 'user', 'key').
        timeout: Network connect timeout in seconds.

    Returns:
        Configured Fabric Connection instance.
    """
    config = Config()
    config.missing_host_key_policy = AutoAddPolicy()

    key_path = os.path.expanduser(str(vm["key"]))

    return Connection(
        host=vm["ip"],
        user=vm["user"],
        connect_kwargs={"key_filename": key_path},
        connect_timeout=timeout,
        config=config,
    )


def get_vm_free_space_gb(conn: Connection, vm_name: str) -> int:
    """Query available disk space in gigabytes on the remote VM root partition.

    Args:
        conn: Active Fabric SSH connection.
        vm_name: Display name of the VM for logging purposes.

    Returns:
        Available space in gigabytes, or 0 if query fails.
    """
    try:
        result = conn.run("df -BG --output=avail /", hide=True, warn=True)
        if not result.ok:
            return 0
        output = result.stdout
    except Exception as exc:
        logger.warning(f"[{vm_name}] Space check failed: {exc}")
        return 0

    for line in output.splitlines():
        clean = "".join(c for c in line.strip() if c.isdigit())
        if clean:
            return int(clean)
    return 0


def list_remote_files(sftp: Any, directory: str) -> list[str]:
    """List non-hidden files in a remote directory via SFTP.

    Args:
        sftp: Active SFTP client instance.
        directory: Remote directory path to inspect.

    Returns:
        List of filenames found, or an empty list if directory is empty or missing.
    """
    try:
        return [f for f in sftp.listdir(directory) if not f.startswith(".")]
    except OSError:
        return []


def remove_remote_file(sftp: Any, remote_path: str) -> bool:
    """Safely delete a file on a remote VM via SFTP.

    Args:
        sftp: Active SFTP client instance.
        remote_path: Full remote file path to delete.

    Returns:
        True if the file was deleted, False if deletion failed or file did not exist.
    """
    try:
        sftp.remove(remote_path)
        return True
    except OSError:
        return False


def move_remote_file(conn: Connection, src: str, dst: str) -> bool:
    """Atomically move/rename a remote file via SSH.

    Args:
        conn: Active Fabric SSH connection.
        src: Source path on the remote host.
        dst: Destination path on the remote host.

    Returns:
        True if the file was moved successfully, False otherwise.
    """
    try:
        result = conn.run(f'mv "{src}" "{dst}"', hide=True, warn=True)
        return bool(result.ok)
    except Exception:
        return False
