# Transcode Subsystem

The **Transcode Subsystem** offloads heavy video encoding to remote Linux cloud instances to convert source videos to space-efficient **AV1** using SVT-AV1 (`libsvtav1`) and Opus audio (`libopus`).

---

## Features

* **Strict Sequential Staging:** Utilizes a 4-tier numbered directory pipeline (`01` through `04`) to prevent processing partially transferred files.
* **Self-Updating Engine:**
  * Auto-updates the script itself via `git pull`.
  * Auto-updates the latest static BtbN FFmpeg master builds (supporting both `x86_64` and `aarch64` ARM architectures).
* **Compression Optimization Guard:** Compares the transcoded file size against the original input file. If the transcoded output is not smaller, the transcode is discarded and the original file is preserved.
* **Non-Blocking Resource Limits:** Enforces low process priority (`nice -n 18`) and caps multi-threaded execution to preserve instance stability.
* **Multi-VM Load Balancing:** The local synchronization script balances upload distribution based on remote queue depth and available free disk space.

---

## Remote Pipeline Directory Flow

```text
Local Source Video
       │ (WinSCP Upload)
       ▼
01_upload_staging/     <-- Incoming partial uploads (.filepart filtered out)
       │ (Atomic mv when upload finishes)
       ▼
02_transcode_queue/    <-- Files waiting for the transcode cron job
       │ (Picked up by transcode.sh)
       ▼
03_transcode_staging/  <-- Temporary working directory for FFmpeg encoding
       │ (Successful encode verified)
       ▼
04_transcode_finished/ <-- Completed AV1 video waiting for download
       │ (WinSCP Download)
       ▼
Local Destination Video (Local original deleted on successful download)
```

---

## Remote Server Setup

### 1. Clone Repository
Log in to your cloud instance and execute:

```bash
cd /home/ubuntu
git clone https://github.com/YourUsername/media-pipeline.git
cd media-pipeline/transcode
chmod +x transcode.sh
```

### 2. Setup Cron Job
Add an idempotent cron job that triggers `transcode.sh` every minute. Overlapping runs are blocked using `flock`.

```bash
(crontab -l 2>/dev/null | grep -Fv "/home/ubuntu/media-pipeline/transcode/transcode.sh"; \
 echo "* * * * * /home/ubuntu/media-pipeline/transcode/transcode.sh") | crontab -
```

---

## Local Windows Setup

### 1. Configuration
Copy `config_transcode.yaml.example` to `config_transcode.yaml` and adjust paths:

```bash
cp transcode/config_transcode.yaml.example transcode/config_transcode.yaml
```

Update your directories, VM IP addresses, and SSH keys in `config_transcode.yaml`. By default, WinSCP binaries are automatically loaded from `../common/winscp.com`.

### 2. Run Local Synchronization
Execute the synchronization script on your local Windows PC:

```powershell
python transcode/sync_transcoded.py
```