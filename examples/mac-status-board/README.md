# Mac status board

ESP32-D0WD-V3 / ST7796, 320×480 portrait. This optional display consumes the
Worker Desk schema-2 `host-board.json` snapshot, and also supports the standalone
host collectors. Version 0.4.0 adds authenticated LAN discovery and device-initiated
TCP telemetry, reconnects automatically, and retains HTTP push / USB compatibility.

Team and Personal Token Plan allowances appear as separate quota rows. Kimi
account total and code-monthly windows are shown when supplied, alongside weekly
and short-window allowance. Missing readings are never replaced with zero.

The renderer, firmware and desktop preview share the same drawing code. The
progress strip targets 60 Hz; this is distinct from full-screen frame rate.
Inspect the actual timing/counter fields in `/status` for device performance.

See [API.md](API.md) for discovery, authentication, network scope and telemetry.
Secrets belong only in the device NVS and an owner-only local token file; none
are included here. Keep the telemetry and OTA services on your trusted LAN.

## Build and verify

Use Python 3.9 or newer, CMake and SDL2 for the desktop preview. The host needs
psutil; firmware builds use PlatformIO Core 6.1.19 and the pinned platform/library
versions in `platformio.ini`. The LTO hook under `scripts/` is part of the build.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-host.txt platformio==6.1.19
.venv/bin/python -m platformio run
cmake -S preview -B .build-preview -DCMAKE_BUILD_TYPE=Release
cmake --build .build-preview -j 6
.build-preview/board_tests
.venv/bin/python -m unittest discover -s tests -v
```

The firmware uses the existing 4 MB dual-OTA partition layout. Before a real
deployment, retain your known-good firmware and host scripts, render a preview
from your current snapshot, then verify the device's version, received quota
rows, connection state and actual refresh counters. Device credentials, backups,
live snapshots, screenshots and firmware binaries are deliberately excluded from
this source example.

When the local MacStatusBoard LaunchAgent is already installed, use
`host/ota_upload.py firmware.bin --pause-agent` to pause only that agent during
OTA and restore it even after upload failure or interruption. Omit the flag on
a first deployment without that agent. Keep a known-good image for recovery.
