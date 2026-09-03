"""Common data models, configuration loaders, and path utilities."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Recognized video file extensions across all pipeline stages
VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg")


@dataclass
class VMStatus:
    """Snapshot of a remote VM state, active queues, and free storage.

    Attributes:
        vm: Configuration dictionary for the VM containing credentials and host info.
        free_space_gb: Available disk space in gigabytes on the root filesystem.
        queued_files: Files waiting in the input queue.
        staging_out_files: Files currently being processed in staging.
        completed_files: Finished files ready for download.
        staging_in_files: In-flight or interrupted upload files.
        extra: Subsystem-specific metadata (e.g., per-model queue mappings).
    """

    vm: dict[str, Any]
    free_space_gb: int
    queued_files: list[str] = field(default_factory=list)
    staging_out_files: list[str] = field(default_factory=list)
    completed_files: list[str] = field(default_factory=list)
    staging_in_files: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def get_file_stem(filename: str) -> str:
    """Extract a clean stem from a filename, stripping extensions and ``.filepart``.

    The original casing of the filename is preserved so that generated output
    filenames (e.g. subtitle deliverables) retain the same casing as the source
    video. Callers that need case-insensitive comparisons should normalize the
    result themselves (e.g. via ``.lower()``).

    Args:
        filename: Name of the file (e.g., 'videoA.mkv.filepart', 'videoA.mkv', or a full path).

    Returns:
        Stem string with extension and any trailing '.filepart' marker removed (e.g., 'videoA').
    """
    clean_name = os.path.basename(filename)
    if clean_name.lower().endswith(".filepart"):
        clean_name = clean_name[:-9]
    return os.path.splitext(clean_name)[0]


def resolve_video_stem(filename: str, shortcode: str) -> str:
    """Resolve the originating video's stem from a subtitle pipeline artifact name.

    Plain video files (queued or in-flight uploads) map directly via
    :func:`get_file_stem`. Subtitle-stage artifacts, however, embed the model's
    full shortcode or a ``tmp_`` prefix in their name:

    * Finished subtitles: ``<stem>.<shortcode>.srt``
    * In-progress stage markers: ``<stem>.<shortcode>.stage``
    * Intermediate working directories: ``tmp_<stem>``

    Naively taking the stem of these names (stripping only the final extension)
    yields ``<stem>.<shortcode>`` or ``tmp_<stem>`` rather than ``<stem>``, which
    then fails to match the stem of the originating video. This function strips
    those known suffixes/prefixes first so the result is always comparable to a
    raw video filename's stem.

    Args:
        filename: Name of a remote file or directory associated with a model queue.
        shortcode: Full model shortcode used to suffix subtitle artifacts.

    Returns:
        Stem string matching the originating video's stem, for use in set membership checks.
    """
    clean_name = os.path.basename(filename)
    lower_name = clean_name.lower()

    srt_suffix = f".{shortcode}.srt".lower()
    if lower_name.endswith(srt_suffix):
        return clean_name[: -len(srt_suffix)]

    stage_suffix = f".{shortcode}.stage".lower()
    if lower_name.endswith(stage_suffix):
        return clean_name[: -len(stage_suffix)]

    tmp_prefix = "tmp_"
    if lower_name.startswith(tmp_prefix):
        return clean_name[len(tmp_prefix) :]

    return get_file_stem(clean_name)


def load_yaml_config(config_path: str | Path) -> dict[str, Any]:
    """Load and validate a YAML configuration file from disk.

    Args:
        config_path: Relative or absolute path to the YAML configuration file.

    Returns:
        Loaded configuration dictionary.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        ValueError: If the file content is not a valid YAML dictionary mapping.
    """
    resolved_path = Path(config_path).expanduser().resolve()

    if not resolved_path.is_file():
        msg = f"Configuration file not found at: {resolved_path}"
        raise FileNotFoundError(msg)

    with open(resolved_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        msg = f"Configuration root in {resolved_path.name} must be a YAML mapping."
        raise ValueError(msg)

    return config
