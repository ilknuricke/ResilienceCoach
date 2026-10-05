# Resilience Sense

The local sensing and session server of ResilienceCoach: a web dashboard running on
Windows. It is built to take several wearables. The first supported device is a
WHOOP 4.0, connected through OpenStrap's reference Python client
(`../openstrap-research/research_playground.py`, unmodified). The client handles the BLE
protocol, the sync/ACK state machine and the decoders.

## Run

Double-click **`start-lan.bat`** (shared on the local network) or **`start.bat`** (this PC
only). The server runs in the background with no window, and the page opens in your
browser. **`stop.bat`** stops it. Log: `data/server.log`.

What keeps a run alive:

- **Supervisor** (`supervise.py`): restarts the server about 3 s after any exit or crash.
- **Session resume**: the run state (`data/runstate.json`) is saved on every change. After a
  restart, an open session is re-opened (gap recorded as `session_resumed`), a calibration
  continues in the right step with the right time left, and live mode reconnects.
  Interruptions longer than 15 min close the session instead (`session_interrupted`).
- **Bluetooth auto-reconnect**: if the band drops while live is on, the server reconnects
  every 10 s and restarts the streams.
- **Off-wrist pause**: the band stops its sensors on WRIST_OFF; the server restarts them.
- **Keep-awake**: Windows won't idle-sleep while a session is recording or live is on.
  Closing the laptop lid, choosing Sleep, or shutting down still stops it.
- Errors in one record or task are logged and skipped, never fatal.

Testing without a wearer: `POST /api/debug/replay?sid=<session>&speed=1` feeds a
recorded session back through the live path. Sessions recorded during a replay are
tagged `replay`, and learning is off, so no one's profile changes.

## Sharing on the local network

Run `start-lan.bat` (or `server.py --lan`). The share link is
`http://resilience.local:8765/?key=<passcode>`. It stays the same on any Wi-Fi because the
server announces `resilience.local` over mDNS and re-announces it when the PC's IP changes.
Set `SENSE_MDNS_NAME` to use a different name. The console also prints an IP-based
fallback link, for devices that can't resolve `.local` names (some older Android phones).

- On this PC (localhost) you get full control, with no passcode.
- Anyone else needs the passcode link and is **view-only**. They see live HR, history
  and the daily table. Connect, sync, live and buzz are refused.
  Set `SENSE_REMOTE_CONTROL=1` to give them control too.
- The passcode is `share_key` in `data/config.json`. Delete that entry and restart to
  rotate it.
- Collaborators need this PC's firewall to allow inbound Python. On guest or corporate
  Wi-Fi, "client isolation" can block device-to-device traffic entirely.

## Connecting the band

1. Close the WHOOP phone app, or turn off Bluetooth on your phone. Only one device can
   hold the band at a time.
2. Take the band off your wrist and double-tap it so it advertises.
3. Click **Connect**. Accept the Windows pairing prompt if one appears.
4. **Sync now** downloads the band's stored history. **Start live** streams heart rate.

OpenStrap's warning: once you use this, don't go back to the official WHOOP app with the
same band. A firmware update could change the records this depends on.

## Changes on top of the reference client

- `Store` persists the 1 Hz R24 records (HR and RR intervals) into a deduplicated `r24`
  table. Upstream `WhoopStore` drops them because of a `kind` name mismatch.
- Commits are batched every 2 s instead of once per frame.
- Live mode always uses wrist-gated optical. It never forces the LEDs on or sets the
  persistent flag.

The daily metrics are simple approximations: 5th-percentile resting HR, cleaned RMSSD,
and the reference client's TRIMP strain and recovery formulas. They aren't WHOOP's
scores, and they aren't OpenStrap's full Dart analytics.

## Exercise sessions

Click **Start live**, then **Start session**. The first phase is a resting baseline (aim
for 2 min). Use **Effort** and **Rest**, a custom marker, or double-tap the band to switch
phases. Collaborators on the share link see it all live, in view-only mode.

Updated every second from the band's live R10 record (HR plus the 100 Hz accelerometer,
4096 units = 1 g, verified):

- **HR and zone**: zones are 50/60/70/80/90 % of max HR. Max HR is what you enter, or
  208 − 0.7 × age (Tanaka 2001), or 190 by default.
- **Intensity**: ENMO in milli-g (mean of |a| − 1 g), the standard wrist-accelerometer
  metric.
- **Rhythm**: repeats/min of the movement over the last 6 s, from autocorrelation. It
  reports the per-step rate rather than the arm-swing rate. Blank when the movement
  isn't regular.
- **Per phase**: mean and peak HR, change vs. baseline, intensity, rhythm, and time in
  each zone. **HRR60** is computed after every effort → rest switch.

Sessions are stored in `data/sessions.db`. The "past sessions" list has a CSV export
(per-second samples, phase table and HRR).

Live beat-to-beat intervals aren't streamed by this band in live mode, so there's no live
HRV yet. The live 0x28 "on-wrist" flag reads false while worn, so it isn't shown.

### What each session saves (`data/sessions.db`)

| Table | Content | Timestamps |
|---|---|---|
| `session_samples` | per-second HR, zone, intensity, rhythm, phase | host time + band clock |
| `session_raw` | **every live record verbatim** (motion+HR R10, optical R11, compact HR 0x28, events, command replies) | host receive time + band clock |
| `session_markers` | phase markers (button, double-tap, calibration) | host time |
| `session_events` | band events (wrist on/off, double-tap…), calibration steps, learned max/rest HR, live-data gaps and restarts, session start/stop with settings and final summary | host time |

Downloads per session (Past sessions list): **summary CSV** (per-second data, markers,
phases, HRR), **~104 Hz motion CSV** (accelerometer in g plus raw gyro, one row per
sample) and **raw JSONL** (every record in hex, plus events, so you can re-decode
everything later). Expect roughly 15 MB per hour of session.

If the band reports WRIST_OFF it shuts off its live sensors. The server notices (no data
for 6 s, or a WRIST_ON event), restarts the streams, and records the gap. Wear the band
snug, about a finger's width above the wrist bone, to avoid false wrist-off events.

## Calibration

Pick **Calibration** in the session-type menu and press **Start session**. It's one
11-minute guided session: 3 min seated rest, then 3 min easy, 2 min hard, 1 min as hard
as you safely can, and 2 min standing recovery. The band buzzes at each step. It learns:

- **Resting HR**: the lowest 30-s mean HR during a still baseline. The median of the
  last 10 baselines is used, and every session's baseline contributes.
- **Max HR**: the highest HR sustained for 10 s (steady within 12 bpm, and not equal to
  the movement rhythm, to avoid cadence lock). It only ever rises. Until then it's a
  180 placeholder.

Manual overrides take precedence. Age is not used.
