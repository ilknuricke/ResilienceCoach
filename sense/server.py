"""
Resilience Sense: the local sensing and session server of ResilienceCoach.
The first supported device is a WHOOP 4.0 band, through OpenStrap's reference client
(../openstrap-research/research_playground.py). The BLE protocol, sync/ACK state
machine and decoders are theirs, unmodified; this file only adds an HTTP/WebSocket
layer and a few read-only queries over the SQLite store the client writes.

Run:  .venv\\Scripts\\python server.py   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "openstrap-research"))
import research_playground as rp  # noqa: E402
import session as session_mod  # noqa: E402

if sys.platform == "win32" and rp.BleakClient is not None:
    # Windows serves a cached GATT table on reconnect, which is often stale right after
    # the previous link drops ("characteristic 61080003 not found"). Force a fresh discovery.
    import functools
    rp.BleakClient = functools.partial(rp.BleakClient, winrt={"use_cached_services": False})

DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "whoop.db"
def env(name: str, default=None):
    """Setting SENSE_<name>; the older WHOOP_<name> spelling is still accepted."""
    return os.environ.get("SENSE_" + name, os.environ.get("WHOOP_" + name, default))


# Raw BLE capture (JSONL) is the canonical, re-decodable record but grows fast.
CAPTURE_PATH = DATA_DIR / "whoop_capture.jsonl" if env("CAPTURE") == "1" else None
CONFIG_PATH = DATA_DIR / "config.json"

# ── stable LAN name: advertise <MDNS_NAME>.local over mDNS and follow IP changes ──
LAN = False
MDNS_NAME = env("MDNS_NAME", "resilience")
PORT = int(os.environ.get("PORT", 8765))


def current_ip() -> Optional[str]:
    import socket
    try:
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.connect(("8.8.8.8", 80))  # no packet is sent; just picks the outbound interface
        ip = s_.getsockname()[0]
        s_.close()
        return ip
    except Exception:
        return None


async def _mdns_loop():
    import socket
    from zeroconf import IPVersion, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    azc, info, ip = None, None, None
    try:
        while True:
            new_ip = current_ip()
            if new_ip != ip:
                if azc:  # network changed: rebind sockets to the new interface
                    try:
                        await azc.async_unregister_all_services()
                        await azc.async_close()
                    except Exception:
                        pass
                    azc = None
                ip = new_ip
                if ip:
                    try:
                        azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
                        info = ServiceInfo("_http._tcp.local.", "Resilience Sense._http._tcp.local.",
                                           addresses=[socket.inet_aton(ip)], port=PORT,
                                           server=f"{MDNS_NAME}.local.", properties={"path": "/"})
                        await azc.async_register_service(info, allow_name_change=True)
                        print(f"mDNS: {MDNS_NAME}.local -> {ip}", flush=True)
                        hub.note(f"share link: http://{MDNS_NAME}.local:{PORT}/?key=... (now {ip})")
                    except Exception as e:
                        print(f"mDNS failed: {e}", flush=True)
                        azc = None
            await asyncio.sleep(10)
    finally:
        if azc:
            try:
                await azc.async_unregister_all_services()
                await azc.async_close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(_app):
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(
        lambda l, ctx: _log_exc("async", ctx.get("exception") or Exception(ctx.get("message"))))
    restore_runstate()
    tasks = [asyncio.create_task(_flush_loop()), asyncio.create_task(_live_watchdog())]
    if LAN:
        tasks.append(asyncio.create_task(_mdns_loop()))
    yield
    SHUTTING_DOWN[0] = True
    save_runstate()                                # keep the open session resumable
    if CAL.get("task"):
        CAL["task"].cancel()
    for t in tasks:
        t.cancel()
    if hub.client:
        await hub.client.disconnect()  # turns optical/persistent flags back off
    if hub.store:
        hub.store.flush()


app = FastAPI(title="Resilience Sense", lifespan=lifespan)


# ── LAN sharing: localhost = full control; anyone else needs the passcode and is
#    view-only unless SENSE_REMOTE_CONTROL=1. ─────────────────────────────────
LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}
REMOTE_CONTROL = env("REMOTE_CONTROL") == "1"


def share_key() -> str:
    cfg = load_config()
    if not cfg.get("share_key"):
        save_config(share_key=secrets.token_urlsafe(9))
        cfg = load_config()
    return cfg["share_key"]


def _is_local(host: Optional[str]) -> bool:
    return (host or "") in LOCAL_IPS


def _authorized(host, cookies, query) -> bool:
    if _is_local(host):
        return True
    key = share_key()
    return secrets.compare_digest(cookies.get("wk", ""), key) or         secrets.compare_digest(query.get("key", ""), key)


def can_control(host) -> bool:
    return _is_local(host) or REMOTE_CONTROL


@app.middleware("http")
async def access_guard(request: Request, call_next):
    host = request.client.host if request.client else None
    if not _authorized(host, request.cookies, request.query_params):
        return PlainTextResponse("Missing or wrong passcode. Open the full share link from the host (it ends in ?key=...).", 401)
    if request.method == "POST" and not can_control(host):
        return JSONResponse({"detail": "view-only: only the host can control the band"}, 403)
    resp = await call_next(request)
    if "key" in request.query_params and not _is_local(host):
        # Serve the page directly and set the cookie on this response; no redirect.
        # (A redirect + SameSite=Strict cookie is dropped when the link is opened from
        # another app, e.g. Messages/WhatsApp, which looks like a cross-site navigation.)
        # The page strips ?key= from the address bar itself.
        resp.set_cookie("wk", share_key(), httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.get("/api/whoami")
async def whoami(request: Request):
    host = request.client.host if request.client else None
    return {"control": can_control(host), "local": _is_local(host)}


# ── state ──────────────────────────────────────────────────────────────────
class Hub:
    def __init__(self):
        self.client: Optional[rp.WhoopClient] = None
        self.store: Optional[rp.WhoopStore] = None
        self.sockets: set[WebSocket] = set()
        self.busy: Optional[str] = None          # "connecting" | "syncing" | None
        self.live = False
        self.last_hr: Optional[int] = None
        self.on_wrist: Optional[bool] = None
        self.wrist: Optional[bool] = None      # from WRIST_ON/OFF events (authoritative)
        self.live_since = 0.0
        self.last_resume = 0.0
        self.stalled = False
        self.want_live = False                 # user intent: keep live streaming (survives drops/restarts)
        self.last_reconnect = 0.0
        self.log: list[dict] = []

    @property
    def connected(self) -> bool:
        c = self.client
        return bool(c and c.client and c.client.is_connected)

    def status(self) -> dict:
        c = self.client
        return {
            "connected": self.connected,
            "address": c.address if c else load_config().get("address"),
            "busy": self.busy,
            "live": self.live,
            "battery_pct": c.battery_pct if c else None,
            "charging": c.charging if c else None,
            "hello": rp.asdict(c.hello) if c and c.hello else None,
            "sync_records": c._sync_records if c else 0,
            "sync_events": c._sync_events if c else 0,
            "sync_complete": c.sync_complete if c else False,
            "hr": self.last_hr,
            "on_wrist": self.on_wrist,
            "wrist": self.wrist,
            "stalled": self.stalled,
            "replay": REPLAY["sid"],
        }

    def broadcast(self, msg: dict):
        data = json.dumps(msg, default=str)
        for ws in list(self.sockets):
            asyncio.ensure_future(_safe_send(ws, data, self))

    def note(self, text: str):
        entry = {"t": time.time(), "text": text}
        self.log = (self.log + [entry])[-200:]
        self.broadcast({"type": "log", **entry})


async def _safe_send(ws: WebSocket, data: str, hub: Hub):
    try:
        await ws.send_text(data)
    except Exception:
        hub.sockets.discard(ws)


hub = Hub()


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def on_decode(d: dict):
    kind = d.get("kind")
    if kind == "realtime_hr":
        hr = d.get("hr") or None
        hub.last_hr, hub.on_wrist = hr, d.get("on_wrist")
        hub.broadcast({"type": "hr", "hr": hr, "on_wrist": hub.on_wrist, "t": time.time()})
    elif kind == "event":
        hub.note(f"event: {d.get('event')}")
        SESSION.record_event("band_event", {k: v for k, v in d.items() if k not in ("crc_ok",)})
        if d.get("event") in ("WRIST_ON", "WRIST_OFF"):
            hub.wrist = d["event"] == "WRIST_ON"
            if hub.wrist and hub.live:
                # the band shuts its live sensors off at WRIST_OFF and doesn't restart them
                asyncio.ensure_future(_resume_live("band back on wrist"))
        if d.get("event") == "DOUBLE_TAP" and SESSION.active:
            m = SESSION.toggle("double_tap")   # hands-free effort <-> rest marker
            if m:
                hub.broadcast({"type": "marker", **m})
        hub.broadcast({"type": "status", **hub.status()})
    elif kind == "cmd_response" and ("battery_pct" in d or "hello" in d):
        hub.broadcast({"type": "status", **hub.status()})


class Store(rp.WhoopStore):
    """
    The reference store, with two fixes:
      * it never persisted the 1 Hz R24 records (decoder emits kind 'R24_telemetry',
        store matched 'R24_recovery') — those carry HR + RR, so they go in their own
        deduplicated table here (re-syncs re-drain the whole flash);
      * it committed every frame individually; we skip the per-frame hex table
        (the JSONL capture covers that) and commit on a timer instead.
    """
    def _init_db(self):
        super()._init_db()
        self.db.execute("""CREATE TABLE IF NOT EXISTS r24(
            ts INTEGER PRIMARY KEY, hr INTEGER, rr TEXT, decoded TEXT)""")
        self.db.commit()
        self._real_commit, self.db_dirty = self.db.commit, False

    def record_frame(self, direction, char, frame):
        pass

    def record_decoded(self, decoded: dict):
        if decoded.get("kind") == "R24_telemetry" and decoded.get("ts_epoch"):
            self.db.execute("INSERT OR IGNORE INTO r24(ts,hr,rr,decoded) VALUES(?,?,?,?)",
                            (decoded["ts_epoch"], decoded.get("hr"),
                             json.dumps(decoded.get("rr_intervals_ms") or []), json.dumps(decoded)))
            self.db_dirty = True
            return
        if decoded.get("kind") == "realtime_hr":
            return  # live HR is shown, not stored; history comes from R24
        super().record_decoded(decoded)

    def flush(self):
        if self.db_dirty:
            self.db.commit()
            self.db_dirty = False


async def _resume_live(reason: str):
    if not (hub.connected and hub.live) or time.time() - hub.last_resume < 8:
        return
    hub.last_resume = time.time()
    hub.note(f"live data stopped ({reason}): re-enabling streams")
    SESSION.record_event("live_resume", {"reason": reason})
    try:
        await hub.client.enable_live_streams(hr=True, imu=True, optical=True)
    except Exception as e:
        hub.note(f"re-enable failed: {type(e).__name__}")


_AWAKE = [None]


def _keep_awake(on: bool):
    """Stop Windows sleeping while a session records or live is wanted (released otherwise)."""
    if sys.platform != "win32" or _AWAKE[0] == on:
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    _AWAKE[0] = on


async def _live_watchdog():
    """If live mode is on but no motion/HR record arrived for 6 s, mark stalled and re-enable."""
    while True:
        await asyncio.sleep(2)
        try:
            _keep_awake(bool(SESSION.active) or hub.want_live)
            # Bluetooth dropped (out of range, Windows hiccup) while live was wanted: reconnect.
            if (hub.want_live and not hub.connected and not hub.busy and not REPLAY["task"]
                    and time.time() - hub.last_reconnect > 10):
                hub.last_reconnect = time.time()
                SESSION.record_event("ble_reconnect_attempt", {})
                hub.note("band not connected: reconnecting...")
                try:
                    await _ensure_connected()
                    await hub.client.enable_live_streams(hr=True, imu=True, optical=True)
                    hub.live, hub.live_since = True, time.time()
                    SESSION.record_event("ble_reconnected", {})
                    hub.note("band reconnected, live resumed")
                except Exception as e:
                    hub.note(f"reconnect failed ({type(e).__name__}); retrying in 10 s")
                hub.broadcast({"type": "status", **hub.status()})
                continue
        except Exception as e:
            _log_exc("watchdog", e)
        if not (hub.live and hub.connected) or hub.busy or REPLAY["task"]:
            if hub.stalled:
                hub.stalled = False
                hub.broadcast({"type": "status", **hub.status()})
            continue
        last = max(SESSION.last["t"] if SESSION.last else 0, hub.live_since)
        stalled = time.time() - last > 6
        if stalled != hub.stalled:
            hub.stalled = stalled
            SESSION.record_event("live_gap_start" if stalled else "live_gap_end",
                                 {"wrist": hub.wrist, "last_data": last})
            hub.broadcast({"type": "status", **hub.status()})
        if stalled:
            await _resume_live("no data for 6 s" + (" (band reports off wrist)" if hub.wrist is False else ""))


async def _flush_loop():
    while True:
        await asyncio.sleep(2)
        if hub.store:
            try:
                hub.store.flush()
            except Exception as e:
                print("flush error", e)


class Client(rp.WhoopClient):
    """The reference client, with its log lines also sent to the web UI."""
    def _log(self, *a):
        text = " ".join(str(x) for x in a).strip()
        try:
            print(text, flush=True)
        except Exception:
            pass
        # per-second live records would flood the web log
        if text and not text.startswith(("« R10", "« realtime_hr", "« R21")):
            hub.note(text)

    def _on_frame(self, role, frame):
        try:
            super()._on_frame(role, frame)
        except Exception as e:                    # never let one odd record stop the stream
            _log_exc("decode", e)
        if not (frame.crc8_ok and frame.crc32_ok):
            return
        if REPLAY["task"]:                        # a replay owns the session feed
            return
        if (PROBE["until"] > time.time()
                and frame.packet_type not in (rp.PacketType.METADATA, rp.PacketType.HISTORICAL_DATA)):
            PROBE["fh"].write(json.dumps({"t": time.time(), "role": role, "hex": frame.inner.hex()}) + "\n")
        handle_live(frame.inner)


def _log_exc(where: str, e: Exception):
    import traceback
    msg = f"{where} error: {type(e).__name__}: {e}"
    try:
        print(msg + "\n" + traceback.format_exc(), flush=True)
    except Exception:
        pass
    hub.note(msg)


def handle_live(inner: bytes, t: Optional[float] = None):
    """One live record -> session capture + per-second tick. Shared by the band and replay."""
    try:
        pt = inner[0] if inner else -1
        # Full-fidelity session capture: every live record, verbatim, with host + band time.
        # (Historical sync records and sync markers are not part of a live session.)
        if SESSION.active and pt not in (rp.PacketType.METADATA, rp.PacketType.HISTORICAL_DATA):
            SESSION.record_raw(t or time.time(), inner)
        # Live R10 = HR + 100 Hz accelerometer, once a second -> session tick
        if pt == rp.PacketType.REALTIME_RAW_DATA and len(inner) > 1 and inner[1] == 10:
            rec = rp.parse_r10(inner)
            if rec:
                tick = SESSION.on_r10(rec)
                hub.broadcast({"type": "tick", **tick})
                if SESSION.active and int(tick["t"]) % 3 == 0:
                    hub.broadcast({"type": "summary", **SESSION.summary()})
    except Exception as e:
        _log_exc("live", e)


# ── replay: feed a recorded session back through the live path (testing without a wearer) ──
REPLAY = {"task": None, "sid": None}


async def _replay(sid: int, speed: float):
    rows = SESSION.db.execute("SELECT t, data FROM session_raw WHERE session_id=? ORDER BY t", (sid,)).fetchall()
    hub.note(f"replay of session {sid} started ({len(rows)} records, x{speed})")
    try:
        prev = rows[0][0] if rows else 0
        for t, data in rows:
            await asyncio.sleep(max(0.0, (t - prev) / speed))
            prev = t
            inner = bytes(data)
            if inner and inner[0] == rp.PacketType.EVENT:
                try:
                    on_decode(rp.decode_frame(rp.Frame(raw=b"", gen=rp.Gen.HARVARD, inner=inner, crc8_ok=True, crc32_ok=True)))
                except Exception:
                    pass
            handle_live(inner)
        hub.note(f"replay of session {sid} finished")
    finally:
        REPLAY.update(task=None, sid=None)
        hub.broadcast({"type": "status", **hub.status()})


@app.post("/api/debug/replay")
async def api_replay(sid: int, speed: float = 1.0, stop: bool = False):
    if stop:
        if REPLAY["task"]:
            REPLAY["task"].cancel()
        return {"ok": True}
    if REPLAY["task"]:
        raise HTTPException(409, "replay already running")
    REPLAY.update(sid=sid, task=asyncio.create_task(_replay(sid, max(0.1, min(speed, 20)))))
    hub.broadcast({"type": "status", **hub.status()})
    return {"ok": True}


# Raw recorder for validating live streams (units, rates) before using them.
PROBE = {"until": 0.0, "fh": None}


@app.post("/api/debug/probe/{seconds}")
async def api_probe(seconds: int):
    if PROBE["fh"]:
        PROBE["fh"].close()
    PROBE["fh"] = open(DATA_DIR / "probe.jsonl", "w", buffering=1)
    PROBE["until"] = time.time() + min(seconds, 600)
    return {"recording_until": PROBE["until"]}


# ── exercise sessions ──────────────────────────────────────────────────────
# Max/resting HR are learned from the data (see Session._learn_max/_learn_rest), not
# from age. Learned max only ever rises, so the placeholder sits low-ish and gets pushed
# up by the first hard effort; a manual value always wins.
HR_MAX_PLACEHOLDER = 180
HR_REST_PLACEHOLDER = 60


# ── people: every learned/manual HR value belongs to a profile, never to the band ──
PROFILE_KEYS = ("hr_max", "hr_rest", "hr_max_seen", "hr_max_seen_t", "hr_rest_seen")


def profiles() -> dict:
    cfg = load_config()
    if not cfg.get("profiles"):
        save_config(profiles={"p1": {"name": "Person 1"}}, active_profile="p1")
        cfg = load_config()
    return cfg["profiles"]


def active_pid() -> str:
    ps = profiles()
    pid = load_config().get("active_profile")
    return pid if pid in ps else next(iter(ps))


def prof(pid: Optional[str] = None) -> dict:
    return profiles()[pid or active_pid()]


def save_prof(pid: Optional[str] = None, **kw):
    ps = profiles()
    pid = pid or active_pid()
    ps[pid] = {**ps[pid], **kw}
    save_config(profiles=ps)


def predicted_max(age, sex) -> tuple[Optional[float], Optional[str]]:
    """Age-predicted max HR. Women: Gulati et al. 2010 (Circulation, n=5437 women);
    otherwise Tanaka et al. 2001 (JACC). Individual error is roughly +/-10 bpm either way."""
    if not age:
        return None, None
    if sex == "F":
        return round(206 - 0.88 * age, 1), "206 - 0.88 x age (Gulati 2010, women)"
    return round(208 - 0.7 * age, 1), "208 - 0.7 x age (Tanaka 2001)"


def _max_source(cfg) -> str:
    seen = cfg.get("hr_max_seen") or 0
    pred, _ = predicted_max(cfg.get("age"), cfg.get("sex"))
    if cfg.get("hr_max"):
        return "manual"
    if pred:
        return "measured" if seen > pred else "age-predicted"
    return "measured" if seen >= 150 else "placeholder"


def _hr_profile(pid: Optional[str] = None) -> tuple[float, float]:
    cfg = prof(pid)
    seen = cfg.get("hr_max_seen") or 0
    pred, _ = predicted_max(cfg.get("age"), cfg.get("sex"))
    if cfg.get("hr_max"):
        hr_max = cfg["hr_max"]
    elif pred:
        # A measured peak can only push max HR up: an under-read or a light effort must
        # never pull a person's zones down below what their age predicts.
        hr_max = max(pred, seen)
    else:
        # No age: a sustained >=150 bpm means a real hard effort was measured.
        hr_max = seen if seen >= 150 else max(HR_MAX_PLACEHOLDER, seen)
    rests = cfg.get("hr_rest_seen") or []
    hr_rest = cfg.get("hr_rest") or (round(statistics.median(rests), 1) if rests else HR_REST_PLACEHOLDER)
    return float(hr_max), float(hr_rest)


SESSION = session_mod.Session(DATA_DIR / "sessions.db", *_hr_profile())  # separate file: no lock contention with sync


def _session_broadcast():
    hub.broadcast({"type": "session", **SESSION.snapshot()})


@app.get("/api/session")
async def api_session():
    return SESSION.snapshot()


@app.post("/api/session/start")
async def api_session_start(name: str = "", protocol: str = ""):
    """protocol="" -> free session; "full" / "rest" / "exercise" -> guided calibration session."""
    if protocol:
        return await api_calibrate(protocol, name)
    if CAL["task"]:
        raise HTTPException(409, "a calibration session is running")
    SESSION.start(name, "replay" if REPLAY["task"] else active_pid())
    save_runstate()
    _session_broadcast()
    return SESSION.state()


@app.post("/api/session/mark")
async def api_session_mark(label: str = "", kind: str = "effort"):
    if kind not in ("effort", "rest"):
        raise HTTPException(400, "kind must be effort or rest")
    try:
        m = SESSION.mark(label, kind)
    except ValueError as e:
        raise HTTPException(409, str(e))
    hub.broadcast({"type": "marker", **m})
    return m


@app.post("/api/session/stop")
async def api_session_stop():
    if CAL["task"]:                 # stopping a calibration session = cancel its timer too
        CAL["task"].cancel()
        return {"cancelled": True}
    summary = SESSION.stop()
    save_runstate()
    _session_broadcast()
    return summary or {}


@app.get("/api/sessions")
async def api_sessions():
    return SESSION.list_sessions()


@app.get("/api/session/{sid}")
async def api_session_get(sid: int):
    samples, markers = SESSION.load(sid)
    row = SESSION.db.execute("SELECT profile, hr_max, hr_rest FROM sessions WHERE id=?", (sid,)).fetchone()
    pid = row[0] if row else None
    return {"samples": samples, "markers": markers, "summary": SESSION.summary(samples, markers),
            "counts": SESSION.counts(sid),
            # whose session this is: their current profile, plus the values in effect when it ran
            "profile": settings_payload(pid) if pid in profiles() else None,
            "profile_id": pid,
            "at_time": {"hr_max": row[1], "hr_rest": row[2]} if row else None}


@app.get("/api/session/{sid}/imu.csv")
async def api_session_imu(sid: int):
    return StreamingResponse(SESSION.imu_csv(sid), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="session_{sid}_imu_100hz.csv"'})


@app.get("/api/session/{sid}/raw.jsonl")
async def api_session_raw(sid: int):
    return StreamingResponse(SESSION.raw_jsonl(sid), media_type="application/x-ndjson", headers={
        "Content-Disposition": f'attachment; filename="session_{sid}_raw.jsonl"'})


@app.get("/api/session/{sid}/csv")
async def api_session_csv(sid: int):
    return PlainTextResponse(SESSION.csv(sid), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="session_{sid}.csv"'})


def _on_learn(kind: str, value: float, t: float):
    # learned values go to the person this session belongs to
    pid = (SESSION.active or {}).get("profile") or active_pid()
    if REPLAY["task"] or pid == "replay":     # replayed data must never change a person's values
        SESSION.record_event("learned_ignored_replay", {"kind": kind, "value": value}, t)
        return
    SESSION.record_event("learned", {"kind": kind, "value": value, "profile": pid}, t)
    cfg = prof(pid)
    if kind == "hr_max":
        old = SESSION.hr_max
        save_prof(pid, hr_max_seen=value, hr_max_seen_t=t)
        SESSION.hr_max, SESSION.hr_rest = _hr_profile()
        if SESSION.hr_max != old:
            hub.note(f"max HR learned: {old:.0f} -> {SESSION.hr_max:.0f} bpm (sustained 10 s)")
    elif kind == "hr_rest":
        rests = (cfg.get("hr_rest_seen") or [])[-9:] + [value]
        save_prof(pid, hr_rest_seen=rests)
        SESSION.hr_max, SESSION.hr_rest = _hr_profile()
        hub.note(f"resting HR from this baseline: {value:.0f} bpm (using median of "
                 f"{len(rests)}: {SESSION.hr_rest:.0f})")
    hub.broadcast({"type": "settings", **settings_payload()})


SESSION.on_learn = _on_learn
SESSION.hr_max_seen = prof().get("hr_max_seen")


def _activate_profile(pid: str):
    save_config(active_profile=pid)
    SESSION.hr_max, SESSION.hr_rest = _hr_profile()
    SESSION.hr_max_seen = prof().get("hr_max_seen")


def settings_payload(pid: Optional[str] = None) -> dict:
    """The active person's profile, or any person's (pid) for viewing a past session."""
    pid = pid or active_pid()
    cfg = prof(pid)
    rests = cfg.get("hr_rest_seen") or []
    hr_max, hr_rest = (SESSION.hr_max, SESSION.hr_rest) if pid == active_pid() else _hr_profile(pid)
    return {"profile": pid, "profile_name": cfg.get("name"),
            "profiles": [{"id": k, "name": v.get("name")} for k, v in profiles().items()],
            "hr_max": hr_max, "hr_rest": hr_rest,
            "hr_max_manual": cfg.get("hr_max"), "hr_rest_manual": cfg.get("hr_rest"),
            "hr_max_seen": cfg.get("hr_max_seen"), "hr_max_seen_t": cfg.get("hr_max_seen_t"),
            "hr_max_default": HR_MAX_PLACEHOLDER, "n_baselines": len(rests),
            "age": cfg.get("age"), "sex": cfg.get("sex"),
            "hr_max_pred": predicted_max(cfg.get("age"), cfg.get("sex"))[0],
            "hr_max_formula": predicted_max(cfg.get("age"), cfg.get("sex"))[1],
            "hr_max_source": _max_source(cfg)}


# ── guided calibration: timed phases, auto-markers, band buzz at each change ──
PROTOCOLS = {
    "full": [   # one session: seated rest (learns resting HR) straight into the exercise steps
        ("Baseline", "rest", 180, "Sit still, relaxed, breathing normally. Don't talk."),
        ("Warm-up", "effort", 180, "Stand up and go easy: march, shuffle or jog gently. You could chat."),
        ("Build", "effort", 120, "Pick it up to hard. Talking gets difficult."),
        ("Peak", "effort", 60, "As hard as you safely can for 60 s. Stop if dizzy or in pain."),
        ("Recovery", "rest", 120, "Stop and stand still. Recovery is being measured."),
    ],
    "rest": [
        ("Baseline", "rest", 180, "Sit still, relaxed, breathing normally. Don't talk."),
    ],
    "exercise": [
        ("Baseline", "rest", 60, "Stand still and relaxed."),
        ("Warm-up", "effort", 180, "Easy pace: march, shuffle or jog gently. You could chat."),
        ("Build", "effort", 120, "Pick it up to hard. Talking gets difficult."),
        ("Peak", "effort", 60, "As hard as you safely can for 60 s. Stop if dizzy or in pain."),
        ("Recovery", "rest", 120, "Stop and stand still. Recovery is being measured."),
    ],
}
CAL = {"task": None, "kind": None, "step": None}


def cal_payload() -> dict:
    s = CAL["step"]
    return {"type": "calibration", "running": CAL["task"] is not None, "kind": CAL["kind"],
            **({"step": s} if s else {})}


CAL_NAMES = {"full": "Calibration", "rest": "Calibration · rest", "exercise": "Calibration · exercise"}


async def _run_protocol(kind: str, name: str = "", resume: Optional[tuple] = None):
    """Timed guided session. resume=(step_index, seconds_left) continues after a restart."""
    steps = PROTOCOLS[kind]
    CAL["name"] = name
    try:
        start_i, left = resume or (0, None)
        if not resume:
            SESSION.start(name or CAL_NAMES[kind], "replay" if REPLAY["task"] else active_pid())
        _session_broadcast()
        for i, (label, mkind, secs, text) in enumerate(steps):
            if i < start_i:
                continue
            resumed_step = bool(resume) and i == start_i
            if i > 0 and not resumed_step:
                hub.broadcast({"type": "marker", **SESSION.mark(label, mkind, "calibration")})
            dur = left if resumed_step else secs
            CAL["step"] = {"i": i + 1, "n": len(steps), "label": label, "kind": mkind,
                           "text": text, "ends": time.time() + dur, "secs": secs}
            CAL["step_started"] = time.time() - (secs - dur)
            SESSION.record_event("calibration_step", {**CAL["step"], "resumed": resumed_step})
            save_runstate()
            hub.broadcast(cal_payload())
            if hub.connected:
                try:
                    await hub.client.buzz()       # haptic cue on the wrist
                except Exception:
                    pass
            await asyncio.sleep(dur)
        SESSION.stop()
        hub.note(f"calibration ({kind}) complete")
        if hub.connected:
            try:
                await hub.client.buzz(); await asyncio.sleep(0.6); await hub.client.buzz()
            except Exception:
                pass
    except asyncio.CancelledError:
        if not SHUTTING_DOWN[0]:              # a server shutdown keeps the session open for resume
            SESSION.stop()
            hub.note(f"calibration ({kind}) cancelled")
        raise
    finally:
        if not SHUTTING_DOWN[0]:
            CAL.update(task=None, kind=None, step=None, step_started=None, name=None)
            save_runstate()
            _session_broadcast()
            hub.broadcast(cal_payload())
            hub.broadcast({"type": "settings", **settings_payload()})


# ── run state: what was going on, so a restart can pick it back up ──
RUNSTATE_PATH = DATA_DIR / "runstate.json"
RESUME_WINDOW_S = 15 * 60
SHUTTING_DOWN = [False]


def save_runstate():
    st = {"t": time.time(), "want_live": hub.want_live,
          "session": SESSION.active["id"] if SESSION.active else None,
          "cal": ({"kind": CAL["kind"], "name": CAL.get("name"), "step_i": CAL["step"]["i"] - 1,
                   "step_started": CAL.get("step_started")} if CAL.get("task") and CAL.get("step") else None)}
    try:
        tmp = RUNSTATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, RUNSTATE_PATH)
    except Exception as e:
        print("runstate save failed", e)


def restore_runstate():
    """On startup: re-open an interrupted session (and its calibration timer) if recent."""
    try:
        st = json.loads(RUNSTATE_PATH.read_text())
    except Exception:
        st = {}
    keep = None
    sid = st.get("session")
    if sid:
        row = SESSION.db.execute("SELECT end, start FROM sessions WHERE id=?", (sid,)).fetchone()
        last = SESSION.last_record_t(sid) or (row[1] if row else 0)
        gap = time.time() - last
        if row and row[0] is None and gap < RESUME_WINDOW_S:
            keep = sid
            SESSION.resume(sid, gap)
            hub.note(f"resumed session {sid} after a {gap:.0f} s interruption")
            cal = st.get("cal")
            if cal and cal.get("kind") in PROTOCOLS and cal.get("step_started"):
                steps = PROTOCOLS[cal["kind"]]
                i, into = cal["step_i"], time.time() - cal["step_started"]
                while i < len(steps) and into >= steps[i][2]:      # steps that ran out while down
                    into -= steps[i][2]
                    i += 1
                    if i < len(steps):
                        SESSION.mark(steps[i][0], steps[i][1], "calibration_resume")
                if i >= len(steps):
                    SESSION.stop()
                    keep = None
                    hub.note("calibration time ran out during the interruption; session closed")
                else:
                    CAL["kind"] = cal["kind"]
                    CAL["task"] = asyncio.create_task(
                        _run_protocol(cal["kind"], cal.get("name") or "", resume=(i, steps[i][2] - into)))
    SESSION.close_dangling(keep=keep)
    hub.want_live = bool(st.get("want_live")) or bool(keep)
    save_runstate()


@app.post("/api/calibrate/{kind}")
async def api_calibrate(kind: str, name: str = ""):
    if kind == "cancel":
        if CAL["task"]:
            CAL["task"].cancel()
        return {"ok": True}
    if kind not in PROTOCOLS:
        raise HTTPException(404, "unknown protocol")
    if CAL["task"]:
        raise HTTPException(409, "a calibration is already running")
    if SESSION.active:
        raise HTTPException(409, "stop the current session first")
    if not (hub.live or REPLAY["task"]):
        raise HTTPException(409, "start live mode first")
    CAL["kind"] = kind
    CAL["task"] = asyncio.create_task(_run_protocol(kind, name))
    return {"ok": True}


@app.get("/api/settings")
async def api_settings_get():
    return settings_payload()


@app.post("/api/settings")
async def api_settings(hr_max: Optional[int] = None, hr_rest: Optional[int] = None,
                       reset_learned: bool = False, age: Optional[int] = None,
                       sex: Optional[str] = None):
    """Manual overrides (0 clears one); age/sex of the active person; reset_learned forgets learned values."""
    upd = {}
    if age is not None:
        upd["age"] = age if 10 <= age <= 100 else None
    if sex is not None:
        upd["sex"] = sex if sex in ("F", "M") else None
    if hr_max is not None:
        upd["hr_max"] = hr_max if 120 <= hr_max <= 230 else None
    if hr_rest is not None:
        upd["hr_rest"] = hr_rest if 30 <= hr_rest <= 110 else None
    if reset_learned:
        upd.update(hr_max_seen=None, hr_max_seen_t=None, hr_rest_seen=[])
        SESSION.hr_max_seen = None
    save_prof(**upd)
    SESSION.hr_max, SESSION.hr_rest = _hr_profile()
    hub.broadcast({"type": "settings", **settings_payload()})
    _session_broadcast()
    return settings_payload()


@app.get("/api/profiles")
async def api_profiles():
    return settings_payload()


@app.post("/api/profiles")
async def api_profile_create(name: str):
    name = name.strip()[:40]
    if not name:
        raise HTTPException(400, "name required")
    ps = profiles()
    pid = "p%d" % (max([int(k[1:]) for k in ps if k[1:].isdigit()] or [0]) + 1)
    ps[pid] = {"name": name}
    save_config(profiles=ps)
    return await api_profile_select(pid)


@app.post("/api/profiles/select")
async def api_profile_select(pid: str):
    if pid not in profiles():
        raise HTTPException(404, "no such person")
    if SESSION.active:
        raise HTTPException(409, "stop the current session before switching person")
    _activate_profile(pid)
    hub.note(f"wearer: {prof().get('name')}")
    hub.broadcast({"type": "settings", **settings_payload()})
    _session_broadcast()
    return settings_payload()


@app.post("/api/profiles/rename")
async def api_profile_rename(pid: str, name: str):
    if pid not in profiles() or not name.strip():
        raise HTTPException(400, "bad request")
    save_prof(pid, name=name.strip()[:40])
    hub.broadcast({"type": "settings", **settings_payload()})
    return settings_payload()


@app.post("/api/profiles/relearn")
async def api_profile_relearn(pid: Optional[str] = None):
    """Recompute a person's learned max/resting HR from all their stored sessions."""
    pid = pid or active_pid()
    r = SESSION.relearn(pid)
    save_prof(pid, hr_max_seen=r["hr_max_seen"], hr_max_seen_t=r["hr_max_seen_t"],
              hr_rest_seen=r["hr_rest_seen"])
    if pid == active_pid():
        _activate_profile(pid)
    hub.broadcast({"type": "settings", **settings_payload()})
    return r


@app.post("/api/session/{sid}/assign")
async def api_session_assign(sid: int, pid: str = ""):
    """Set who wore the band for a past session, then relearn the people affected."""
    if pid and pid not in profiles():
        raise HTTPException(404, "no such person")
    old = SESSION.db.execute("SELECT profile FROM sessions WHERE id=?", (sid,)).fetchone()
    SESSION.assign(sid, pid or None)
    for p in {pid, old and old[0]} - {None, ""}:
        if p in profiles():
            await api_profile_relearn(p)
    return {"ok": True}


# ── BLE actions ────────────────────────────────────────────────────────────
def _require_free():
    if hub.busy:
        raise HTTPException(409, f"busy: {hub.busy}")


async def _ensure_connected(address: Optional[str] = None):
    if hub.connected:
        return
    if hub.store is None:
        hub.store = Store(str(DB_PATH), str(CAPTURE_PATH) if CAPTURE_PATH else None)
    address = address or load_config().get("address")
    hub.busy = "connecting"
    hub.broadcast({"type": "status", **hub.status()})
    ok, err = False, None
    try:
        # Windows often reports missing characteristics on the first attempt right after
        # a previous connection drops; release the half-open link and retry.
        for attempt in range(3):
            hub.client = Client(address=address, store=hub.store, on_decode=on_decode, verbose=True)
            try:
                ok = await hub.client.connect()
                break
            except Exception as e:
                err = e
                hub.note(f"connect attempt {attempt + 1} failed: {type(e).__name__}")
                try:
                    if hub.client.client:
                        await hub.client.client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(4)
        else:
            hub.client = None
            raise HTTPException(502, f"connect failed: {type(err).__name__}: {err}")
    finally:
        hub.busy = None
    if not ok:
        hub.client = None
        raise HTTPException(404, "No WHOOP found. Close the WHOOP phone app / turn off phone Bluetooth, "
                                 "take the band off your wrist and double-tap it to advertise.")
    save_config(address=hub.client.address)
    hub.client.start_background_loops()
    await hub.client.get_hello()
    await hub.client.get_battery()
    hub.broadcast({"type": "status", **hub.status()})


@app.post("/api/scan")
async def api_scan():
    _require_free()
    hub.busy = "scanning"
    try:
        c = Client(verbose=True)
        addr = await c.scan(timeout=10)
    finally:
        hub.busy = None
    if addr:
        save_config(address=addr)
    return {"address": addr}


@app.post("/api/connect")
async def api_connect():
    _require_free()
    await _ensure_connected()
    return hub.status()


@app.post("/api/disconnect")
async def api_disconnect():
    if hub.client:
        await hub.client.disconnect()
    hub.client, hub.live, hub.want_live = None, False, False
    save_runstate()
    hub.broadcast({"type": "status", **hub.status()})
    return hub.status()


@app.post("/api/forget")
async def api_forget():
    save_config(address=None)
    return {"ok": True}


@app.post("/api/sync")
async def api_sync():
    _require_free()
    await _ensure_connected()
    c = hub.client
    c.sync_complete, c._sync_records, c._sync_events = False, 0, 0

    async def run():
        hub.busy = "syncing"
        try:
            # send_init ends with SEND_HISTORICAL_DATA; the client ACKs each batch.
            await c.send_init()
            start = idle_since = time.time()
            last = 0
            while not c.sync_complete and time.time() - start < 1800:
                await asyncio.sleep(1.0)
                hub.broadcast({"type": "status", **hub.status()})
                if c._sync_records != last:
                    last, idle_since = c._sync_records, time.time()
                elif time.time() - idle_since > 15:
                    await c.send(rp.Cmd.ABORT_HISTORICAL_TRANSMITS, b"\x00")
                    hub.note("sync went idle — stopped")
                    break
            hub.note(f"sync finished: {c._sync_records} records, complete={c.sync_complete}")
        except Exception as e:
            hub.note(f"sync error: {type(e).__name__}: {e}")
        finally:
            hub.busy = None
            hub.broadcast({"type": "status", **hub.status()})
            hub.broadcast({"type": "synced"})

    asyncio.create_task(run())
    return {"started": True}


@app.post("/api/live/{on}")
async def api_live(on: str):
    _require_free()
    await _ensure_connected()
    if on == "start":
        # Wrist-gated optical only — never force/persist the LEDs from the web UI.
        await hub.client.enable_live_streams(hr=True, imu=True, optical=True)
        hub.live, hub.live_since, hub.want_live = True, time.time(), True
    else:
        await hub.client.disable_live_streams()
        hub.live = hub.want_live = False
    save_runstate()
    hub.broadcast({"type": "status", **hub.status()})
    return hub.status()


@app.post("/api/buzz")
async def api_buzz():
    await _ensure_connected()
    await hub.client.buzz()
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    return hub.status()


# ── history (read-only queries over the client's SQLite store) ─────────────
def _r24_rows(since: float) -> list[tuple]:
    """(ts, hr, rr_list) per device second, from the deduplicated r24 table."""
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute("SELECT ts, hr, rr FROM r24 WHERE ts >= ? ORDER BY ts",
                           (int(since),)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [(ts, hr, json.loads(rr) if rr else []) for ts, hr, rr in rows]


def _rmssd_clean(rr: list[float]) -> Optional[float]:
    rr = [x for x in rr if 300 <= x <= 2000]
    # drop successive jumps >20% (ectopic / missed beats)
    clean = [rr[0]] if rr else []
    for x in rr[1:]:
        if abs(x - clean[-1]) <= 0.2 * clean[-1]:
            clean.append(x)
    return rp.rmssd(clean) if len(clean) >= 20 else None


@app.get("/api/history/hr")
async def history_hr(hours: float = 24, bucket: int = 60):
    rows = _r24_rows(time.time() - hours * 3600)
    acc: dict[int, list[int]] = defaultdict(list)
    for ts, hr, _ in rows:
        if hr:
            acc[ts - ts % bucket].append(hr)
    pts = [{"t": k, "hr": round(sum(v) / len(v), 1), "min": min(v), "max": max(v)}
           for k, v in sorted(acc.items())]
    return {"points": pts, "n_records": len(rows)}


@app.get("/api/history/daily")
async def history_daily(days: int = 14):
    rows = _r24_rows(time.time() - days * 86400)
    by_day: dict[str, dict] = defaultdict(lambda: {"hr": [], "rr": [], "min": defaultdict(list)})
    for ts, hr, rr in rows:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if hr:
            by_day[day]["hr"].append(hr)
            by_day[day]["min"][ts // 60].append(hr)
        by_day[day]["rr"].extend(rr)
    out = []
    for day, d in sorted(by_day.items()):
        hrs = sorted(d["hr"])
        if not hrs:
            continue
        rhr = hrs[max(0, int(len(hrs) * 0.05) - 1)]  # 5th percentile as a resting-HR proxy
        hrv = _rmssd_clean(d["rr"])
        out.append({
            "day": day,
            "minutes_worn": len(hrs) // 60,
            "resting_hr": rhr,
            "mean_hr": round(statistics.fmean(hrs), 1),
            "max_hr": hrs[-1],
            "rmssd_ms": hrv,
            # Banister TRIMP is defined per minute — feed it minute means, not 1 Hz samples.
            "strain_0_21": rp.strain_from_hr_series(
                [sum(v) / len(v) for _, v in sorted(d["min"].items())], rhr=rhr),
            "recovery_0_100": rp.recovery_score(hrv, rhr) if hrv else None,
        })
    return {"days": out}


# ── websocket + static ─────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _authorized(ws.client.host if ws.client else None, ws.cookies, ws.query_params):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.sockets.add(ws)
    await ws.send_text(json.dumps({"type": "status", **hub.status()}, default=str))
    await ws.send_text(json.dumps({"type": "backlog", "log": hub.log[-50:]}, default=str))
    await ws.send_text(json.dumps({"type": "session", **SESSION.snapshot()}, default=str))
    await ws.send_text(json.dumps(cal_payload(), default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.sockets.discard(ws)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    # --lan (or SENSE_HOST=0.0.0.0) shares it on the local network
    host = "0.0.0.0" if "--lan" in sys.argv else env("HOST", "127.0.0.1")
    LAN = host != "127.0.0.1"
    if LAN:
        mode = "with control" if REMOTE_CONTROL else "view-only"
        key = share_key()
        print(f"\nShare link ({mode}) - stays the same on any Wi-Fi:\n"
              f"  http://{MDNS_NAME}.local:{PORT}/?key={key}\n"
              f"Fallback by IP (changes with the network):\n"
              f"  http://{current_ip()}:{PORT}/?key={key}\n", flush=True)
    uvicorn.run(app, host=host, port=PORT, log_level="warning")
