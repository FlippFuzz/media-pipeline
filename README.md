# Media Pipeline

**Media Pipeline** is an automated, distributed media processing framework designed to offload compute-heavy video processing tasks—specifically **AV1 SVT video transcoding** and **multimodal AI subtitle generation**—from a local workstation to remote cloud instances (such as Oracle Cloud Infrastructure Always Free tier virtual machines).

---

## High-Level Concept

```text
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             Local Workstation (Windows)                          │
│                                                                                  │
│   Input Videos ──► [sync_transcoded.py] ──► Upload Video / Download AV1 Video    │
│   Transcoded   ──► [sync_subtitled.py]  ──► Upload Video / Download *.srt Subtitles
└─────────────────────────┬──────────────────────────────────┬─────────────────────┘
                          │ (WinSCP / SFTP / Fabric)          │
                          ▼                                  ▼
         ┌─────────────────────────────────┐┌─────────────────────────────────┐
         │     Remote Worker VM #1         ││      Remote Worker VM #2        │
         │  (Oracle Cloud Free Tier Linux) ││  (Oracle Cloud Free Tier Linux) │
         │                                 ││                                 │
         │  • transcode.sh (SVT-AV1)       ││  • subtitles.sh (g35l-0918)     │
         │  • Self-updates via Git & BtbN  ││  • subtitles.sh (g37f-0918)     │
         │  • Dynamic CPU & core limits    ││  • Self-updates via Git         │
         │  • 4-tier staging pipeline      ││  • 6-stage fallback ladder      │
         └─────────────────────────────────┘└─────────────────────────────────┘
```

1. **Local Intermittent Availability:** The local machine runs synchronization scripts via Windows Task Scheduler or batch scripts only when the workstation is powered on.
2. **Autonomous Cloud Processing:** Remote cloud instances run lightweight cron jobs independently, processing queued tasks, enforcing API quota limits, and managing restricted disk space automatically.
3. **Self-Updating Workers:** Remote worker scripts (`transcode.sh` and `subtitles.sh`) automatically perform self-update checks via `git pull` before running.
4. **Decoupled Workflows:** The video transcoding and subtitle generation subsystems operate independently. They can share the same remote virtual machines or run on separate fleets of VMs.

---

## Subsystems

| Subsystem | Primary Goal | Remote Engine | Local Sync | Documentation |
| :--- | :--- | :--- | :--- | :--- |
| **`transcode/`** | Compress videos to AV1 format using `libsvtav1` | `ffmpeg` (BtbN static builds) via `transcode.sh` | `sync_transcoded.py` | [transcode/README.md](transcode/README.md) |
| **`subtitles/`** | Generate multimodal SRT subtitles using Google Gemini | `ai-sub` via `subtitles_runner.py` & `subtitles.sh` | `sync_subtitled.py` | [subtitles/README.md](subtitles/README.md) |
| **`common/`** | Shared utilities and bundled WinSCP binaries | N/A | Shared package | *Internal library* |

---

## Repository Architecture

```text
media-pipeline/
├── common/                  # Shared utilities
│   ├── models.py            # Dataclasses and configuration parsing
│   ├── ssh.py               # Fabric connection pooling and disk space checks
│   ├── transfer.py          # WinSCP process wrappers and log lifecycle
│   ├── winscp.com           # Bundled WinSCP console binary
│   └── winscp.exe           # Bundled WinSCP engine binary
│
├── transcode/               # Video transcoding pipeline
│   ├── config_transcode.yaml.example
│   ├── sync_transcoded.py   # Windows-to-VM parallel synchronization tool
│   └── transcode.sh         # Linux cron job for AV1 video encoding
│
└── subtitles/               # AI subtitle pipeline
    ├── config_subtitles.yaml.example
    ├── subtitles_runner.py  # AI Sub fallback ladder and orchestration engine
    ├── subtitles.sh         # Linux cron wrapper with flock and auto-updating
    └── sync_subtitled.py    # Windows-to-VM multi-model subtitle sync tool
```

---

## Prerequisites

### Local Workstation (Windows)
* **Python:** 3.10 or newer.
* **WinSCP:** Bundled in `common/` (`winscp.com` and `winscp.exe`).
* **SSH Keys:** An OpenSSH private key (`id_rsa`) for Fabric SSH access and a PuTTY formatted key (`id_rsa.ppk`) for WinSCP authentication.

### Cloud Workers (Oracle Cloud / Linux)
* **Operating System:** Ubuntu 22.04 / 24.04 LTS (x86_64 or aarch64 Ampere A1).
* **Git:** Installed (`sudo apt-get install -y git`).
* **Python:** 3.10 or newer with `venv` installed (`sudo apt-get install -y python3-venv python3-pip`).
* **Cron & Core Utilities:** `cron`, `flock`, `wget`, `tar`, `xz-utils`.

---

## Quick Start Guide

### 1. Set Up Local Python Environment
From the root of the cloned repository on your Windows machine:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install fabric paramiko pyyaml pydantic logfire ai-sub
```

### 2. Configure Subsystems
* To configure the video transcoding pipeline, refer to [transcode/README.md](transcode/README.md).
* To configure the AI subtitling pipeline, refer to [subtitles/README.md](subtitles/README.md).