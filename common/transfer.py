"""WinSCP automation and batch subprocess management."""

from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def escape_winscp_path(path: str | Path) -> str:
    """Escape wildcard mask characters in a path for WinSCP script commands.

    WinSCP treats '[', '*', and '?' as pattern-matching mask characters in commands
    like 'get' and 'put'. Enclosing '[' as '[[]' ensures WinSCP interprets square
    brackets literally and does not treat the path as a wildcard mask.

    Args:
        path: File or directory path to escape.

    Returns:
        Escaped path safe for WinSCP scripting commands.
    """
    return str(path).replace("[", "[[]")


def resolve_winscp_path(custom_path: str | Path | None = None) -> str:
    """Resolve the absolute path to the winscp.com executable.

    Checks the user-provided custom path first. If not provided or not found,
    resolves to the bundled winscp.com binary located inside the common directory.

    Args:
        custom_path: Optional user-configured path to winscp.com.

    Returns:
        Resolved absolute path string to winscp.com.
    """
    if custom_path:
        resolved = Path(custom_path).expanduser().resolve()
        if resolved.is_file():
            return str(resolved)

    bundled = Path(__file__).parent / "winscp.com"
    if bundled.is_file():
        return str(bundled.resolve())

    return "winscp.com"


def cleanup_stale_winscp_logs(log_dir: str | Path | None = None) -> None:
    """Purge orphaned or leftover WinSCP log files from previous sessions.

    Args:
        log_dir: Directory containing WinSCP log files. Defaults to the common directory.
    """
    target_dir = Path(__file__).parent if log_dir is None else Path(log_dir).expanduser().resolve()
    for log_file in target_dir.glob("winscp_*.log"):
        try:
            log_file.unlink(missing_ok=True)
        except OSError:
            pass


def run_winscp_command(
    vm: dict[str, Any],
    command_str: str,
    winscp_path: str,
    log_dir: str | Path | None = None,
) -> bool:
    """Execute a batch transfer command via WinSCP.

    Args:
        vm: Configuration dictionary containing host info and credentials ('key_ppk').
        command_str: WinSCP script command to execute (e.g., 'get' or 'put').
        winscp_path: Path to the winscp.com console executable.
        log_dir: Directory where temporary WinSCP log files should be written.

    Returns:
        True if WinSCP completed successfully (exit code 0), False otherwise.
    """
    vm_name = vm["name"]
    resolved_winscp = Path(winscp_path).expanduser().resolve()

    winscp_engine = resolved_winscp.with_name("winscp.exe")
    if not winscp_engine.is_file():
        logger.error(f"[{vm_name}] winscp.exe engine not found adjacent to {resolved_winscp}")
        return False

    key_path = vm.get("key_ppk")
    if not key_path:
        logger.error(f"[{vm_name}] 'key_ppk' is required for WinSCP transfers.")
        return False

    resolved_key = Path(key_path).expanduser().resolve()
    if not resolved_key.is_file():
        logger.error(f"[{vm_name}] Private key (.ppk) not found: {resolved_key}")
        return False

    target_log_dir = Path(__file__).parent if log_dir is None else Path(log_dir).expanduser().resolve()
    target_log_dir.mkdir(parents=True, exist_ok=True)

    task_id = uuid.uuid4().hex[:8]
    log_path = target_log_dir / f"winscp_{vm_name}_{task_id}.log"

    connection_url = f"sftp://{vm['user']}@{vm['ip']}/"
    winscp_cmd = [
        str(resolved_winscp),
        "/ini=nul",
        "/rawconfig",
        "Interface\\FlushConsole=1",
    ]

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            with subprocess.Popen(
                winscp_cmd,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                bufsize=0,
            ) as proc:
                if proc.stdin:
                    commands = [
                        f'open {connection_url} -privatekey="{resolved_key}" -hostkey=*',
                        "option batch abort",
                        "option confirm off",
                        command_str,
                        "exit",
                        "",
                    ]
                    proc.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                    proc.stdin.flush()
                    proc.stdin.close()

                proc.wait()
                if proc.returncode != 0:
                    logger.error(f"[{vm_name}] WinSCP failed (Exit code {proc.returncode}). See {log_path}")
                    return False

        if log_path.exists():
            log_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        logger.error(f"[{vm_name}] WinSCP execution error: {exc}")
        return False
