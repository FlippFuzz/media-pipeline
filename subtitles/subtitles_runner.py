"""Orchestrator for AI-powered subtitle generation using ai-sub and a 6-stage fallback ladder."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_sub.config import (
    AiSettings,
    DirectorySettings,
    ReEncodeSettings,
    RetrySettings,
    SplittingSettings,
)
from ai_sub.config import (
    Settings as AiSubSettings,
)
from ai_sub.main import ai_sub
from ai_sub.shortcode import generate_full_shortcode

# Ensure monorepo root is on sys.path so common utilities are importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.models import VIDEO_EXTENSIONS, get_file_stem, load_yaml_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class FallbackStage:
    """Configuration for a single fallback escalation stage.

    Attributes:
        stage: Stage index number (not required to be contiguous; only ordering matters).
        re_encode: Whether video chunk re-encoding is enabled.
        fps: Target frame rate for re-encoding, or None if disabled.
        max_runs: Maximum attempt limit for stage tasks.
    """

    stage: int
    re_encode: bool
    fps: float | None
    max_runs: int


DEFAULT_FALLBACK_STAGES: list[FallbackStage] = [
    FallbackStage(stage=1, re_encode=False, fps=None, max_runs=3),
    FallbackStage(stage=2, re_encode=True, fps=1.0, max_runs=6),
    FallbackStage(stage=3, re_encode=True, fps=0.5, max_runs=9),
    FallbackStage(stage=4, re_encode=True, fps=0.1, max_runs=12),
    FallbackStage(stage=5, re_encode=True, fps=0.01, max_runs=15),
    FallbackStage(stage=6, re_encode=True, fps=0.001, max_runs=18),
]


def load_stages(config_path: Path | None) -> list[FallbackStage]:
    """Load fallback stage definitions from configuration or use code defaults.

    Args:
        config_path: Optional path to config_subtitles.yaml.

    Returns:
        List of FallbackStage objects.
    """
    if config_path and config_path.is_file():
        try:
            cfg = load_yaml_config(config_path)
            raw_stages = cfg.get("fallback_stages")
            if raw_stages and isinstance(raw_stages, list):
                stages = [
                    FallbackStage(
                        stage=int(s.get("stage", idx + 1)),
                        re_encode=bool(s.get("re_encode", False)),
                        fps=float(s["fps"]) if s.get("fps") is not None else None,
                        max_runs=int(s.get("max_runs", 3)),
                    )
                    for idx, s in enumerate(raw_stages)
                ]
                return sorted(stages, key=lambda s: s.stage)
        except Exception as exc:
            logger.warning(f"Could not load custom fallback stages ({exc}). Using defaults.")
    return DEFAULT_FALLBACK_STAGES


def get_next_stage_number(stage_numbers: list[int], current: int) -> int | None:
    """Return the next-higher configured stage number after ``current``.

    Args:
        stage_numbers: Sorted list of stage numbers defined in the fallback ladder.
        current: The stage number currently being executed.

    Returns:
        The next stage number in the ladder, or None if ``current`` is the last stage.
    """
    for number in stage_numbers:
        if number > current:
            return number
    return None


def parse_srt_header(srt_path: Path) -> dict[str, Any] | None:
    """Parse JSON metadata header from subtitle block 1 of an SRT file.

    Args:
        srt_path: Path to the generated SRT file.

    Returns:
        Parsed metadata dictionary, or None if parsing fails.
    """
    if not srt_path.is_file():
        return None
    try:
        with open(srt_path, encoding="utf-8") as f:
            content = f.read(4096)

        start = content.find("{")
        end = content.find("}", start)
        if start != -1 and end != -1:
            return json.loads(content[start : end + 1])

        p_start = content.find("(")
        p_end = content.find(")", p_start)
        if p_start != -1 and p_end != -1:
            json_str = "{" + content[p_start + 1 : p_end].strip().rstrip(",") + "}"
            return json.loads(json_str)
    except Exception as exc:
        logger.debug(f"Failed to parse SRT header from {srt_path.name}: {exc}")
    return None


async def run_ai_sub_stage(
    video_path: Path,
    staging_dir: Path,
    tmp_dir: Path,
    model_name: str,
    stage: FallbackStage,
) -> None:
    """Invoke ai-sub with specific fallback stage parameters.

    Args:
        video_path: Path to the input video file.
        staging_dir: Target output directory for generated subtitles.
        tmp_dir: Intermediate workspace directory for chunks.
        model_name: Identifier of the AI model.
        stage: The FallbackStage configuration to apply.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    task_settings = AiSubSettings(
        input_video_file=video_path,
        dir=DirectorySettings(out=staging_dir, tmp=tmp_dir),
        ai=AiSettings(model_subtitles=model_name, model_lyrics=model_name),
        split=SplittingSettings(
            re_encode=ReEncodeSettings(
                enabled=stage.re_encode,
                fps=stage.fps if stage.fps else 1.0,
            )
        ),
        retry=RetrySettings(max_runs=stage.max_runs),
    )
    await ai_sub(task_settings, configure_logging=False)


def process_video(
    video_path: Path,
    dirs: dict[str, Path],
    model_name: str,
    shortcode: str,
    stages: list[FallbackStage],
) -> bool:
    """Process a video through the fallback ladder until complete or terminal.

    Args:
        video_path: Path to the queued video file.
        dirs: Dictionary containing staging and queue directory paths.
        model_name: Identifier of the AI model.
        shortcode: Full model shortcode tag.
        stages: Ordered list of FallbackStage configurations.

    Returns:
        True if the video was finished or reached terminal state, False if aborted.
    """
    stem = get_file_stem(video_path.name)
    srt_filename = f"{stem}.{shortcode}.srt"
    staging_srt = dirs["staging"] / srt_filename
    finished_srt = dirs["finished"] / srt_filename
    stage_file = dirs["staging"] / f"{stem}.{shortcode}.stage"
    tmp_dir = dirs["staging"] / f"tmp_{stem}"

    stage_map = {s.stage: s for s in stages}
    stage_numbers = sorted(stage_map.keys())
    first_stage_number = stage_numbers[0]
    max_stage = stage_numbers[-1]

    current_stage_idx = first_stage_number
    if stage_file.is_file():
        try:
            parsed_stage = int(stage_file.read_text(encoding="utf-8").strip())
            if parsed_stage in stage_map:
                current_stage_idx = parsed_stage
            else:
                logger.warning(
                    f"[{stem}] Saved stage {parsed_stage} is not present in current fallback ladder. "
                    f"Restarting from Stage {first_stage_number}."
                )
        except ValueError:
            current_stage_idx = first_stage_number

    while current_stage_idx <= max_stage:
        stage = stage_map[current_stage_idx]
        logger.info(
            f"[{stem}] Running Stage {stage.stage}/{max_stage} "
            f"(re_encode={stage.re_encode}, fps={stage.fps}, max_runs={stage.max_runs})"
        )

        try:
            asyncio.run(run_ai_sub_stage(video_path, dirs["staging"], tmp_dir, model_name, stage))
        except Exception as exc:
            err_msg = str(exc).lower()
            if "429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg:
                logger.warning(f"[{stem}] Quota limit hit during Stage {stage.stage}: {exc}. Aborting model run.")
                return False
            logger.error(f"[{stem}] Error executing Stage {stage.stage}: {exc}")

        header = parse_srt_header(staging_srt)
        if not header:
            logger.warning(f"[{stem}] No valid SRT generated in Stage {stage.stage}. Advancing stage.")

            next_stage_number = get_next_stage_number(stage_numbers, current_stage_idx)
            if next_stage_number is None:
                # 1E: All stages exhausted without a valid header
                logger.warning(
                    f"[{stem}] All fallback stages exhausted without a complete subtitle. "
                    "Moving available SRT to finished and deleting remote files."
                )
                if staging_srt.is_file():
                    shutil.move(str(staging_srt), str(finished_srt))
                else:
                    finished_srt.write_text(
                        "1\n00:00:00,000 --> 00:00:01,000\n[Transcription failed - All fallback stages exhausted]\n",
                        encoding="utf-8",
                    )
                stage_file.unlink(missing_ok=True)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                video_path.unlink(missing_ok=True)
                return True

            current_stage_idx = next_stage_number
            stage_file.write_text(str(current_stage_idx), encoding="utf-8")
            # Keep tmp_dir intact so ai-sub can resume in the next stage
            continue

        if header.get("complete") is True and header.get("max_retries_exceeded") is False:
            logger.info(f"[{stem}] Successfully completed subtitle generation at Stage {stage.stage}.")
            shutil.move(str(staging_srt), str(finished_srt))
            stage_file.unlink(missing_ok=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            video_path.unlink(missing_ok=True)
            return True

        if header.get("max_retries_exceeded") is True:
            next_stage_number = get_next_stage_number(stage_numbers, current_stage_idx)
            if next_stage_number is not None:
                logger.warning(
                    f"[{stem}] Stage {stage.stage} max retries exceeded. Escalating to Stage {next_stage_number}."
                )
                current_stage_idx = next_stage_number
                stage_file.write_text(str(current_stage_idx), encoding="utf-8")
                # Keep tmp_dir intact so ai-sub can resume and continue working on remaining sections
                continue

            # 1E: Terminal stage exhausted
            logger.warning(
                f"[{stem}] All fallback stages exhausted (Stage {stage.stage} retries exceeded). "
                "Moving partial subtitles to finished and deleting remote files."
            )
            if staging_srt.is_file():
                shutil.move(str(staging_srt), str(finished_srt))
            stage_file.unlink(missing_ok=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            video_path.unlink(missing_ok=True)
            return True

        logger.info(f"[{stem}] Partial progress in Stage {stage.stage}. Preserving for next cron cycle.")
        return True

    return True


def main() -> None:
    """Execute the subtitle runner for the configured model."""
    parser = argparse.ArgumentParser(description="AI Sub subtitle generation runner.")
    parser.add_argument("--model", required=True, help="AI model identifier (e.g. google-gla:gemini-3.5-flash-lite).")
    parser.add_argument("--config", default=None, help="Optional path to config_subtitles.yaml.")
    args = parser.parse_args()

    model_name = args.model
    shortcode = generate_full_shortcode(model_name)
    logger.info(f"Initialized subtitle runner for {model_name} (shortcode: {shortcode})")

    script_dir = Path(__file__).resolve().parent
    model_root = script_dir / "models" / shortcode
    dirs = {
        "staging_in": model_root / "01_upload_staging",
        "queue": model_root / "02_sub_queue",
        "staging": model_root / "03_sub_staging",
        "finished": model_root / "04_sub_finished",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)

    config_file = Path(args.config) if args.config else script_dir / "config_subtitles.yaml"
    stages = load_stages(config_file)

    candidate_files = [f for f in dirs["queue"].iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]

    for video_file in sorted(candidate_files):
        if video_file.name.startswith(".") or video_file.name.endswith(".filepart"):
            continue

        try:
            last_mod = video_file.stat().st_mtime
            if (time.time() - last_mod) < 10:
                continue
        except Exception:
            pass

        logger.info(f"Processing queued video: {video_file.name}")
        success = process_video(video_file, dirs, model_name, shortcode, stages)
        if not success:
            logger.warning(f"Aborting further processing for {model_name} due to quota exhaustion.")
            break


if __name__ == "__main__":
    main()
