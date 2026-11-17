# Raspberry Pi Setup and Benchmark (Phase 2)

Target: **Raspberry Pi 3 Model B+**, **Arducam Camera Module 3 (IMX708)**, Raspberry Pi OS 64-bit (Bookworm or Trixie), headless over SSH. **First bring-up: 2026-11-16** on the owner's Pi 3 B+, running Pi OS on Debian 13 "Trixie" (Python 3.13.5, OpenCV 4.10), booted to the console. Sections 1–4 have now been run on that Pi; sections 5–6 have not. Record anything that differs in this file.

## 1. Hardware checklist
- **Power:** use the official 5.1 V / 2.5 A supply. Under-voltage causes throttling and SD-card corruption. `vcgencmd get_throttled` must report `0x0`.
- **Cooling:** fit a heatsink and a ventilated case. The service runs 24/7, so check the temperature under load (benchmark below).
- **Camera cable:** the Pi 3 B+ CSI connector is the standard 15-pin, 1 mm pitch type. Confirm that the Arducam module ships with, or is used with, a **15-pin to 15-pin** ribbon; some kits include only the 22-pin cable for Pi 5 or Zero. The blue stiffener faces the Ethernet port on the Pi 3.
- **Never hot-plug the CSI ribbon.** Power off before connecting or disconnecting the camera, because it can damage the camera or the Pi. Camera-disconnect tests are done by booting *without* the camera, not by unplugging it live.
- **microSD:** 32 GB, ideally A1/A2 "high endurance". Vigil writes only event clips, a small SQLite database and a bounded journal.

## 2. Flash and first boot
1. Use **Raspberry Pi Imager** → Raspberry Pi 3 → *Raspberry Pi OS Lite (64-bit)*. If you install the desktop image instead, boot it to the console with `sudo systemctl set-default multi-user.target`, because the desktop is sluggish on a Pi 3 and takes CPU and RAM from Vigil. In the settings: set a hostname (e.g. `vigil-pi`), create a user, enable SSH **with public-key authentication only**, configure Wi-Fi, and set the timezone.
2. Boot, then `ssh <user>@vigil-pi.local`, and run:
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo poweroff
   ```
   Connect the camera while it's off: wait for the green LED to stop, unplug the power, attach the ribbon, then boot.
3. Check the camera:
   ```bash
   rpicam-hello --list-cameras          # expect: imx708 ... 4608x2592
   rpicam-still -n -o test.jpg          # then on the Mac: scp vigil-pi.local:test.jpg .
   ```
   Use `test.jpg` to aim the camera at where your dog usually rests. Prefer a slightly elevated view covering the whole resting area, with few windows, TVs or fans in frame.

   **Framing target (ADR-017):** the dog should be at least about **100–120 px long** in the analysis frame (320 px wide by default), roughly a third of the frame width. Keep his usual spots in the lower-middle, not at the bottom edge. If the room is wide, set an analysis crop around the resting area and raise the lores resolution so the crop has real pixels:
   ```toml
   [camera]
   analysis_size = [640, 360]
   [preprocess]
   crop = [0.2, 0.55, 0.55, 0.45]   # x, y, w, h of the resting area (normalized)
   ```
   **Owner's camera:** Arducam IMX708, field of view 75° diagonal × 66° horizontal × 41° vertical (standard lens). Compared with the iPhone 0.5× footage used in DETECTION §5.3 (about 102–108° horizontal), it sees about **half the width and half the height** of the phone view from the same spot. The dog therefore appears about **2× larger** (about 110–120 px instead of about 60 px), meeting the target without a crop. **Aim it at the resting area:** in the phone footage the dog lay at the bottom edge, which is outside the Arducam's view unless the camera is tilted down about 25–30° relative to the phone, or mounted lower or closer.

   **Measure it:** take a still (`rpicam-still -n -o frame.jpg`, 4608 px wide) with the dog in a usual spot, measure his body length in pixels, and multiply by 320/4608 (or by `preprocess.width` / crop width in pixels if cropping). Every usual spot should give **≥ 100 px**. If not, move or tilt the camera first, then consider `preprocess.crop`. Picamera2 uses a full-field-of-view binned sensor mode for 1280x720 video; check `rpicam-hello --list-cameras` and the Vigil startup log if framing looks cropped.

## 3. Install Vigil
```bash
sudo apt install -y git
git clone https://github.com/lawrenceDegoma/Vigil.git ~/Vigil
cd ~/Vigil && sudo deploy/pi/install.sh
```
The installer (idempotent; re-run after `git pull` to update):
- installs `python3-picamera2`, `python3-opencv`, `python3-numpy` and `ffmpeg` from apt;
- creates the `vigil` system user (groups `video`, `render`), `/var/lib/vigil` for data and `/etc/vigil` for config;
- creates `/opt/vigil/venv` with `--system-site-packages` and installs Vigil with `--no-deps`, so pip never replaces apt's NumPy or OpenCV, which Picamera2 is built against (ADR-014);
- installs `/etc/vigil/vigil.toml` and `/etc/vigil/vigil.env` only if they're absent;
- limits the journal to 200 MB and enables `vigil-edge.service`.

Then edit:
- `/etc/vigil/vigil.env`: `VIGIL_DEVICE_ID` defaults to `pi-livingroom`. Keep it, or rename it (1–64 characters, unique per device). It's a label, not a secret.
- `/etc/vigil/vigil.toml`: set `[subject] name`, and `[camera]` (`hflip`/`vflip` if mounted upside down; `autofocus`). Leave detection thresholds at their defaults until fixed-camera footage has been evaluated.

## 4. Benchmark (the Phase 2 gate)
Stop the service first: the camera can only be opened by one process.

```bash
sudo systemctl stop vigil-edge
sudo -u vigil -s            # shell as the vigil user; /var/lib/vigil is private to it (0750)
cd /var/lib/vigil
# 1) analysis cost only (synthetic frames at the lores size), all variants
/opt/vigil/venv/bin/vigil bench -c /etc/vigil/vigil.toml --seconds 60 --json-out bench-offline.json
# 2) live camera + hardware encoder + one test clip (data/bench/), 5 minutes
/opt/vigil/venv/bin/vigil bench -c /etc/vigil/vigil.toml --camera --seconds 300 --json-out bench-camera.json
exit
# copy results somewhere your user can read, then scp them to the Mac
# (the glob must be expanded by root: your user can't list /var/lib/vigil)
sudo sh -c "cp -r /var/lib/vigil/bench-*.json /var/lib/vigil/data/bench $HOME/" && sudo chown -R "$USER": ~/bench*
```
On the Mac: `scp -r vigil-pi:'bench*' docs/benchmarks/`. Rename the JSON files with a date, commit them (not the test clip), and watch the clip. **Decision criteria:**

**Hard gate** (ADR-018). It must pass here and hold through the 24 h soak:

| Measure | Target | If missed |
|---|---|---|
| Delivered fps (camera bench) | ≥ 14.5 of 15 requested, dropped frames < 1 % | look for stalls in the capture loop first (sampler gaps in the report), then lower `analysis_size`, turn off `motion.flow`, or drop to 10 fps (re-validate detection) |
| Analysis ms/frame p95 | below the frame budget (66.7 ms at 15 fps) | try the 240 px / 160 px variants (re-validate detection) |
| Peak RSS | < 250 MB | investigate |
| Test clip | plays at normal speed, includes lead-in, correct framing | check encoder/ffmpeg |

**Warning thresholds** (watch, not blocking): analysis p95 > 40 ms; process CPU > 60 % of one core; temperature ≥ 60 °C or `throttled` ≠ `0x0`. The Pi 3 B+ slows from 1.4 to 1.2 GHz at 60 °C. Add a heatsink and keep the case ventilated. Analysis cost rises with motion in view, so benchmark with a fixed camera and judge with realistic activity.

Only consider a Pi 5 or mini PC if the reduced variants still miss these targets. A Pi 5 has **no hardware H.264 encoder**, so it would need a software-encoder clip backend.

## 5. Run the service
```bash
sudo systemctl start vigil-edge
journalctl -u vigil-edge -f                                   # live logs
/opt/vigil/venv/bin/vigil health --data-dir /var/lib/vigil   # latest heartbeat
sudo -u vigil /opt/vigil/venv/bin/vigil clips list --data-dir /var/lib/vigil
sudo -u vigil /opt/vigil/venv/bin/vigil maintenance on --data-dir /var/lib/vigil   # pause alerts/clips
```
Behavior:
- starts on boot, without waiting for the network;
- restarts on crash (`Restart=always`, no start limit);
- restarted by the systemd watchdog if the capture loop hangs;
- retries a missing camera with backoff up to 60 s, reporting `camera.ok=false` in heartbeats meanwhile.

## 6. Soak and fault tests (Phase 2 exit)
Record results in `docs/benchmarks/`.
- [ ] **24 h soak:** `vigil health` every few hours. Check fps stays steady, memory doesn't grow, temperature, `throttled`, and that `/var/lib/vigil` grows only with event clips.
- [ ] **Reboot recovery:** `sudo reboot`, then confirm the service is up and the camera is OK within about 60 s.
- [ ] **Power loss:** pull the power during operation, then confirm it boots cleanly, the database is intact (`sqlite3 /var/lib/vigil/vigil.db 'pragma integrity_check'`), and clips are indexed.
- [ ] **No camera at boot:** power off, disconnect the ribbon, boot. Logs should show retries and the heartbeat `camera.ok=false`, with no crash loop. Power off, reconnect, boot.
- [ ] **Process kill:** `sudo systemctl kill -s KILL vigil-edge`; it should restart within about 5 s.
- [ ] **Low disk:** set `disk_quota_gb` small, generate clips (e.g. wave at the camera), and confirm the oldest unpreserved clips are deleted and preserved ones kept.
- [ ] **Maintenance:** `maintenance on` stops incident export and clips; heartbeat shows `maintenance: true`.

## 7. Notes and known constraints
- **No real-time clock.** The Pi 3 restores the last saved time at boot (fake-hwclock) until NTP syncs. Detection uses monotonic time and is unaffected, but UTC timestamps in clips and events may be off until sync after an offline boot. Phase 3 will flag unsynced clocks (ADR-016).
- **Security:**
  - SSH keys only (`PasswordAuthentication no`).
  - No port forwarding to the Pi; nothing listens on the network.
  - Consider `ufw default deny incoming` plus `ufw allow from <LAN> to any port 22`.
- **Footage on the device** lives only in `/var/lib/vigil/clips` (mode 0750, user `vigil`), bounded by retention and quota.
