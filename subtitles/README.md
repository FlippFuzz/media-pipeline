# Subtitles Subsystem

The **Subtitles Subsystem** provides distributed, multimodal AI subtitle generation powered by `ai-sub` and Google Gemini models. It uses an isolated multi-model architecture with an automated 6-stage fallback escalation ladder.

---

## Key Design Principles

### 1. Model Isolation & Deterministic Quotas
Different Gemini models have vastly different free-tier daily request limits:
* **`gemini-3.5-flash-lite`:** ~500 requests/day free tier.
* **`gemini-3.7-flash`:** ~20 requests/day free tier.

To prevent quota starvation and lockouts, each model operates in its own isolated directory named after its `full_shortcode` generated directly via `ai_sub.shortcode` (e.g., `models/g35l-0918/` and `models/g37f-0918/`), running on dedicated cron schedules:
* **Flash-Lite (`g35l-0918`):** Scheduled 4 times per day (e.g., every 6 hours), processing up to 4 queued videos per run.
* **Flash (`g37f-0918`):** Scheduled once per day (e.g., at 01:00 UTC right after daily quota reset), processing 1 queued video per run.

### 2. Strict Disk Space Management (20 GB Limit)
Free-tier cloud instances typically have only 15–25 GB of usable root volume space. This subsystem enforces strict bounds:
* **Per-Model Queue Caps:** Local synchronization strictly caps the number of active video files enqueued per model (e.g., 4 for `flash-lite`, 1 for `flash`).
* **Immediate Chunk Purging:** Intermediate chunks and re-encoded frames in `tmp_<stem>` are deleted as soon as a model finishes its pass.
* **Ephemeral Source Videos:** The source video is purged from that model's remote queue immediately after that model completes its transcribing pass.

### 3. Self-Updating Worker Wrapper
`subtitles.sh` checks for git repository updates on the worker before running jobs, keeping the subtitle runner up to date with the repository automatically.

---

## The 6-Stage Fallback Escalation Ladder

If an AI transcription run encounters difficult audio/video sequences and exceeds retries, `ai-sub` records `max_retries_exceeded: true` in subtitle block 1 of the output SRT header.

The `subtitles_runner.py` inspects this header and automatically escalates across a 6-stage ladder:

| Stage | Re-encode Enabled | Frame Rate (`fps`) | Max Runs (`retry.max-runs`) | Strategy Description |
| :---: | :---: | :---: | :---: | :--- |
| **1** | `False` | Native / None | `3` | **Fast pass:** Processes original video directly with zero re-encoding. |
| **2** | `True` | `1.0` | `6` | **Light re-encode:** Downsamples video to 1 frame per second. |
| **3** | `True` | `0.5` | `9` | **Medium re-encode:** Downsamples video to 1 frame every 2 seconds. |
| **4** | `True` | `0.1` | `12` | **Dense downsample:** Downsamples video to 1 frame every 10 seconds. |
| **5** | `True` | `0.01` | `15` | **Coarse downsample:** 1 frame every 100 seconds (focuses on audio tokens). |
| **6** | `True` | `0.001` | `18` | **Final effort:** Near audio-only equivalent sampling. |
| **Terminal** | — | — | — | **Give up:** Preserves whatever partial SRT was assembled and marks complete. |

*Note: The fallback ladder can be customized via `fallback_stages` in `config_subtitles.yaml` or will default to the above ladder in code.*

---

## Remote Directory Structure Using Full Shortcodes

Each model maintains an isolated queue structure based on its `full_shortcode` (`<model_code>-<lyrics_v><sub_v>`):

```text
/home/ubuntu/media-pipeline/subtitles/
├── models/
│   ├── g35l-0918/             # Gemini 3.5 Flash-Lite workspace
│   │   ├── 01_upload_staging/
│   │   ├── 02_sub_queue/
│   │   ├── 03_sub_staging/
│   │   └── 04_sub_finished/   # Holds *.g35l-0918.srt
│   │
│   └── g37f-0918/             # Gemini 3.7 Flash workspace
│       ├── 01_upload_staging/
│       ├── 02_sub_queue/
│       ├── 03_sub_staging/
│       └── 04_sub_finished/   # Holds *.g37f-0918.srt
│
├── subtitles.sh               # Universal shell wrapper with flock & git self-update
└── subtitles_runner.py        # Parameterized python runner invoking ai-sub
```

---

## Remote Server Setup

### 1. Clone Repository and Setup Virtual Environment
Log in to your cloud instance and execute:

```bash
cd /home/ubuntu
git clone https://github.com/YourUsername/media-pipeline.git
cd media-pipeline/subtitles

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install ai-sub pyyaml

chmod +x subtitles.sh
```

### 2. Configure API Keys (`.env`)
Create `/home/ubuntu/media-pipeline/subtitles/.env` with your credentials:

```bash
AISUB_AI_GOOGLE_KEY="AIzaSyYourGoogleApiKeyHere"
```

### 3. Setup Crontab Schedules
Open crontab (`crontab -e`) and add independent schedules for each model:

```bash
# Gemini 3.5 Flash-Lite (g35l-0918): Run 4 times a day (every 6 hours)
0 0,6,12,18 * * * /home/ubuntu/media-pipeline/subtitles/subtitles.sh google-gla:gemini-3.5-flash-lite >> /home/ubuntu/subtitles_g35l.log 2>&1

# Gemini 3.7 Flash (g37f-0918): Run once a day at 01:00 UTC (right after daily quota resets)
0 1 * * * /home/ubuntu/media-pipeline/subtitles/subtitles.sh google-gla:gemini-3.7-flash >> /home/ubuntu/subtitles_g37f.log 2>&1
```

---

## Local Windows Setup

### 1. Configuration
Copy `config_subtitles.yaml.example` to `config_subtitles.yaml`:

```bash
cp subtitles/config_subtitles.yaml.example subtitles/config_subtitles.yaml
```

Update your directories, VM connection settings, target models, and queue limits.

### 2. Run Local Subtitle Synchronization
Execute the synchronization script on your local Windows PC:

```powershell
python subtitles/sync_subtitled.py
```

### How Synchronization Operates:
1. **Target Evaluation:** Inspects each local video in `input_dir` and checks if the corresponding model shortcode SRT file (e.g., `video.g35l-0918.srt` or `video.g37f-0918.srt`) exists in `output_dir` (resolving shortcodes via `ai_sub.shortcode`).
2. **Download Phase:** Collects and downloads all completed `.srt` files from all remote `04_sub_finished/` folders via WinSCP, then cleans up the remote copies.
3. **Queue Balancing:** Identifies videos missing specific model SRTs and uploads them to available VMs that have not exceeded `max_queue_per_vm` for that specific model directory.