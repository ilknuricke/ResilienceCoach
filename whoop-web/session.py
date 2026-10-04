"""
Real-time exercise session engine.

Per second it turns the band's live R10 record (HR + 100 Hz accelerometer) into:
  hr        bpm, from the record (matches the 0x28 live HR stream)
  enmo_mg   movement intensity: mean(max(|a| - 1 g, 0)) in milli-g, the standard
            wrist-accelerometry metric (ENMO, as used by GGIR / UK Biobank)
  rhythm    repeats/min of the movement over the last 6 s, from the autocorrelation
            of |a|. Picks the per-impact rate (≈ steps/min when walking, marching or
            shuffling), not the slower arm-swing cycle. None when the movement isn't
            periodic enough to call.
  zone      1–5 by % of max HR (50/60/70/80/90 %)

A session is a baseline phase followed by marker-defined phases. Each phase gets
mean/peak HR, change vs. baseline, mean intensity and rhythm, and time in zone; every
effort→rest transition gets heart-rate recovery at 60 s (HRR60).
"""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import time
from collections import deque
from typing import Optional

import numpy as np

# Each live R10 record carries 100 contiguous accelerometer samples, but records arrive every
# 0.9612 s (band clock sec+subsec/32768 and host receive times agree; no overlap between
# records), so the true rate is ~104 Hz, not 100. Measured 2026-10-04 on 3 sessions.
SAMPLES_PER_RECORD = 100
RECORD_PERIOD_S = 0.9612
FS = SAMPLES_PER_RECORD / RECORD_PERIOD_S   # ~104.04 samples/s
LSB_PER_G = 4096            # verified: |a| ≈ 4096 at rest
RHYTHM_WIN_S = 6
ZONE_EDGES = (0.5, 0.6, 0.7, 0.8, 0.9)


def zone_for(hr: Optional[float], hr_max: float) -> int:
    if not hr:
        return 0
    f = hr / hr_max
    return sum(f >= e for e in ZONE_EDGES)   # 0 = below zone 1


def rhythm_per_min(mag_g: np.ndarray) -> Optional[float]:
    """Dominant periodicity of |a| (20–240 cycles/min) via normalized autocorrelation."""
    x = mag_g - mag_g.mean()
    if x.std() < 0.03:                       # too still to have a rhythm
        return None
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0]
    lo, hi = int(FS * 60 / 240), int(FS * 60 / 20)   # lag range for 240..20 per min
    seg = ac[lo:hi]
    peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] > seg[i + 1]]
    if not peaks:
        return None
    best = max(seg[i] for i in peaks)
    if best < 0.3:                           # weak periodicity — don't guess
        return None
    # The arm-swing cycle (lag 2L) and each footfall (lag L) both correlate strongly,
    # so argmax flips between them. Take the shortest lag that is nearly as strong:
    # consistently the per-step (impact) rate.
    k = next(i for i in peaks if seg[i] >= max(0.5 * best, 0.3))
    lag = lo + k
    # parabolic refinement of the peak
    if 0 < lag < n - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        d = (a - c) / (2 * (a - 2 * b + c)) if (a - 2 * b + c) != 0 else 0
        lag = lag + d
    return round(60 * FS / lag, 1)


class Session:
    def __init__(self, db_path, hr_max: float, hr_rest: float):
        self.db_path = str(db_path)
        self.hr_max, self.hr_rest = hr_max, hr_rest
        self.mag = deque(maxlen=int(FS * RHYTHM_WIN_S))
        self.active: Optional[dict] = None    # {id, start, samples[], markers[]}
        self.last: Optional[dict] = None
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        con = self._con()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, start REAL, end REAL,
            name TEXT, hr_max REAL, hr_rest REAL);
        CREATE TABLE IF NOT EXISTS session_samples(session_id INTEGER, t REAL, hr INTEGER,
            enmo_mg REAL, rhythm REAL, zone INTEGER, phase TEXT);
        CREATE TABLE IF NOT EXISTS session_markers(session_id INTEGER, t REAL, label TEXT,
            kind TEXT, source TEXT);
        -- every live BLE record received during the session, verbatim (re-decodable):
        -- t = host receive time, ts_device = band clock (s) where the record carries one
        CREATE TABLE IF NOT EXISTS session_raw(session_id INTEGER, t REAL, ts_device INTEGER,
            packet_type INTEGER, rec_type INTEGER, data BLOB);
        -- band events, calibration steps, learned-value updates, anything else timestamped
        CREATE TABLE IF NOT EXISTS session_events(session_id INTEGER, t REAL, kind TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS ix_raw ON session_raw(session_id, t);
        CREATE INDEX IF NOT EXISTS ix_samples ON session_samples(session_id, t);
        """)
        for ddl in ("ALTER TABLE session_samples ADD COLUMN ts_device INTEGER",   # band clock
                    "ALTER TABLE sessions ADD COLUMN profile TEXT"):               # who wore the band
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        con.commit()

    def last_record_t(self, sid: int) -> Optional[float]:
        r = self.db.execute("SELECT MAX(t) FROM session_raw WHERE session_id=?", (sid,)).fetchone()[0]
        return r or self.db.execute("SELECT MAX(t) FROM session_samples WHERE session_id=?", (sid,)).fetchone()[0]

    def resume(self, sid: int, gap_s: float) -> dict:
        """Re-open a session that was cut off by a server restart; the gap is recorded."""
        row = self.db.execute("SELECT start, name, profile FROM sessions WHERE id=?", (sid,)).fetchone()
        samples, markers = self.load(sid)
        self._hr_win = None
        self.active = {"id": sid, "start": row[0], "name": row[1], "profile": row[2],
                       "samples": samples, "markers": markers}
        self.record_event("session_resumed", {"gap_s": round(gap_s, 1)})
        return self.state()

    def close_dangling(self, keep: Optional[int] = None):
        """Sessions cut off by a server kill and not resumed: close at their last record, note why."""
        con = self.db
        for (sid,) in con.execute("SELECT id FROM sessions WHERE end IS NULL AND id IS NOT ?", (keep,)).fetchall():
            last = con.execute("SELECT MAX(t) FROM session_raw WHERE session_id=?", (sid,)).fetchone()[0] \
                or con.execute("SELECT MAX(t) FROM session_samples WHERE session_id=?", (sid,)).fetchone()[0] \
                or con.execute("SELECT start FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            con.execute("UPDATE sessions SET end=? WHERE id=?", (last, sid))
            con.execute("INSERT INTO session_events VALUES(?,?,?,?)",
                        (sid, last, "session_interrupted",
                         json.dumps({"note": "server stopped mid-session; closed at last record on restart"})))
        con.commit()

    def _con(self):
        return self.db

    # ── learning max / resting HR from the data itself ──
    # on_learn(kind, value, t) is set by the server to persist and announce updates.
    on_learn = None
    hr_max_seen: Optional[float] = None
    _hr_win: deque = None

    def _learn_max(self, hr, rhythm, t):
        """
        Raise the observed max HR when a higher value is *sustained*: the median of the
        last 10 s must beat the current best, the window must be steady (spread ≤ 12 bpm,
        so not a spike), and it must not equal the movement rhythm — wrist optical HR can
        lock onto step cadence during vigorous movement. Only ever goes up.
        """
        if self._hr_win is None:
            self._hr_win = deque(maxlen=10)
        self._hr_win.append(hr)
        w = self._hr_win
        if len(w) < w.maxlen or any(v is None for v in w):
            return
        med = sorted(w)[len(w) // 2]
        if med > 220 or max(w) - min(w) > 12:
            return
        if rhythm and abs(med - rhythm) <= 4:
            return
        if self.hr_max_seen is None or med > self.hr_max_seen:
            self.hr_max_seen = med
            if self.on_learn:
                self.on_learn("hr_max", med, t)

    def _learn_rest(self, baseline_samples):
        """Resting HR from a baseline: lowest 30-s mean HR while still (ENMO < 50 mg). Needs at
        least 2 min of still data, so short standing baselines (e.g. after exercise) don't count."""
        hrs = [s["hr"] for s in baseline_samples if s["hr"] and s["enmo_mg"] < 50]
        if len(hrs) < 120:
            return
        best = min(sum(hrs[i:i + 30]) / 30 for i in range(len(hrs) - 29))
        if self.on_learn:
            self.on_learn("hr_rest", round(best, 1), time.time())

    # ── per-second processing ──
    def on_r10(self, rec) -> dict:
        """rec: research_playground.R10. Returns the per-second tick."""
        ax = np.asarray(rec.accel_x, float)
        ay = np.asarray(rec.accel_y, float)
        az = np.asarray(rec.accel_z, float)
        mag = np.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
        self.mag.extend(mag.tolist())
        enmo = float(np.maximum(mag - 1.0, 0).mean() * 1000)
        rhythm = rhythm_per_min(np.asarray(self.mag)) if len(self.mag) >= FS * 3 else None
        hr = rec.hr or None
        tick = {"t": time.time(), "ts_device": rec.ts_epoch, "hr": hr, "enmo_mg": round(enmo, 1),
                "rhythm": rhythm, "zone": zone_for(hr, self.hr_max)}
        self._learn_max(hr, rhythm, tick["t"])
        if self.active:
            tick["phase"] = self.active["markers"][-1]["label"]
            self.active["samples"].append(tick)
            con = self._con()
            con.execute("INSERT INTO session_samples(session_id,t,hr,enmo_mg,rhythm,zone,phase,ts_device) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (self.active["id"], tick["t"], hr, tick["enmo_mg"], rhythm,
                         tick["zone"], tick["phase"], rec.ts_epoch))
            con.commit()
        self.last = tick
        return tick

    # ── full-fidelity capture ──
    def record_raw(self, t: float, inner: bytes):
        """Store one live record verbatim. Committed with the next per-second tick."""
        if not self.active:
            return
        pt = inner[0] if inner else -1
        rt = inner[1] if len(inner) > 1 else -1
        ts = None
        if pt == 0x2B and len(inner) >= 11:          # R10/R11/R17: u32 seconds at [7:11]
            ts = int.from_bytes(inner[7:11], "little")
        elif pt == 0x28 and len(inner) >= 6:         # compact realtime: u32 at [2:6]
            ts = int.from_bytes(inner[2:6], "little")
        elif pt == 0x30 and len(inner) >= 8:         # events: u32 at [4:8]
            ts = int.from_bytes(inner[4:8], "little")
        self.db.execute("INSERT INTO session_raw VALUES(?,?,?,?,?,?)",
                        (self.active["id"], t, ts, pt, rt, inner))

    def record_event(self, kind: str, detail=None, t: Optional[float] = None):
        if not self.active:
            return
        self.db.execute("INSERT INTO session_events VALUES(?,?,?,?)",
                        (self.active["id"], t or time.time(), kind, json.dumps(detail, default=str)))
        self.db.commit()

    # ── session control ──
    def start(self, name: str = "", profile: Optional[str] = None) -> dict:
        if self.active:
            self.stop()
        t = time.time()
        con = self._con()
        cur = con.execute("INSERT INTO sessions(start,name,hr_max,hr_rest,profile) VALUES(?,?,?,?,?)",
                          (t, name, self.hr_max, self.hr_rest, profile))
        sid = cur.lastrowid
        con.commit()
        self._hr_win = None                       # don't carry the last wearer's HR window over
        self.active = {"id": sid, "start": t, "name": name, "profile": profile, "samples": [], "markers": []}
        self.record_event("session_start", {"name": name, "profile": profile,
                                            "hr_max": self.hr_max, "hr_rest": self.hr_rest}, t)
        self.mark("Baseline", "rest", "start")
        return self.state()

    def mark(self, label: str, kind: str = "effort", source: str = "button") -> dict:
        if not self.active:
            raise ValueError("no active session")
        m = {"t": time.time(), "label": label[:40] or kind.title(), "kind": kind, "source": source}
        if len(self.active["markers"]) == 1:          # baseline just ended
            self._learn_rest(self.active["samples"])
        self.active["markers"].append(m)
        con = self._con()
        con.execute("INSERT INTO session_markers VALUES(?,?,?,?,?)",
                    (self.active["id"], m["t"], m["label"], m["kind"], source))
        con.commit()
        return m

    def toggle(self, source: str = "double_tap") -> Optional[dict]:
        """Double-tap on the band: flip effort <-> rest."""
        if not self.active:
            return None
        last = self.active["markers"][-1]["kind"]
        n = sum(1 for m in self.active["markers"] if m["kind"] == "effort")
        return self.mark(f"Rest {n}" if last == "effort" else f"Effort {n + 1}",
                         "rest" if last == "effort" else "effort", source)

    def stop(self) -> Optional[dict]:
        if not self.active:
            return None
        if len(self.active["markers"]) == 1:          # session was all baseline
            self._learn_rest(self.active["samples"])
        con = self._con()
        con.execute("UPDATE sessions SET end=? WHERE id=?", (time.time(), self.active["id"]))
        con.commit()
        summary = self.summary()
        self.record_event("session_stop", summary)
        self.db.commit()
        self.active = None
        return summary

    # ── analysis ──
    def summary(self, samples=None, markers=None) -> dict:
        samples = samples if samples is not None else (self.active or {}).get("samples", [])
        markers = markers if markers is not None else (self.active or {}).get("markers", [])
        if not markers:
            return {"phases": [], "hrr": []}
        bounds = [m["t"] for m in markers] + [math.inf]
        phases = []
        for i, m in enumerate(markers):
            ss = [s for s in samples if bounds[i] <= s["t"] < bounds[i + 1]]
            hrs = [s["hr"] for s in ss if s["hr"]]
            rh = [s["rhythm"] for s in ss if s["rhythm"]]
            zones = [0] * 6
            for s in ss:
                zones[s["zone"]] += 1
            phases.append({
                "label": m["label"], "kind": m["kind"], "start": m["t"],
                "dur_s": len(ss),
                "mean_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
                "peak_hr": max(hrs) if hrs else None,
                "mean_enmo_mg": round(sum(s["enmo_mg"] for s in ss) / len(ss), 1) if ss else None,
                "mean_rhythm": round(sum(rh) / len(rh), 1) if rh else None,
                "rhythm_coverage": round(len(rh) / len(ss), 2) if ss else 0,
                "zone_s": zones,
            })
        base = phases[0]["mean_hr"]
        for p in phases:
            p["delta_hr"] = round(p["mean_hr"] - base, 1) if (p["mean_hr"] and base) else None

        def hr_at(t0, half=2.5):
            v = [s["hr"] for s in samples if s["hr"] and abs(s["t"] - t0) <= half]
            return sum(v) / len(v) if v else None

        hrr = []
        for prev, m in zip(markers, markers[1:]):
            if prev["kind"] == "effort" and m["kind"] == "rest":
                h0, h60 = hr_at(m["t"]), hr_at(m["t"] + 60)
                hrr.append({"after": prev["label"], "t": m["t"],
                            "hr_at_stop": round(h0, 1) if h0 else None,
                            "hr_60s": round(h60, 1) if h60 else None,
                            "hrr60": round(h0 - h60, 1) if (h0 and h60) else None})
        return {"phases": phases, "hrr": hrr}

    def state(self) -> dict:
        a = self.active
        return {"active": bool(a), "id": a and a["id"], "start": a and a["start"],
                "name": a and a["name"], "profile": a and a.get("profile"), "markers": a["markers"] if a else [],
                "hr_max": self.hr_max, "hr_rest": self.hr_rest}

    def snapshot(self) -> dict:
        """Everything a late-joining viewer needs to draw the session so far."""
        a = self.active
        return {**self.state(), "samples": a["samples"] if a else [],
                "summary": self.summary() if a else None}

    # ── history / export ──
    def list_sessions(self, limit=20):
        con = self._con()
        rows = con.execute("SELECT id,start,end,name,profile FROM sessions ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
        return [{"id": r[0], "start": r[1], "end": r[2], "name": r[3], "profile": r[4]} for r in rows]

    def assign(self, sid: int, profile: Optional[str]):
        self.db.execute("UPDATE sessions SET profile=? WHERE id=?", (profile, sid))
        self.db.commit()

    def relearn(self, profile: str) -> dict:
        """
        Rebuild a person's learned max/resting HR by replaying the same rules over all their
        stored sessions in order — so values are reproducible and never mix people.
        """
        out = {"hr_max_seen": None, "hr_max_seen_t": None, "hr_max_session": None,
               "hr_rest_seen": [], "hr_rest_sessions": []}
        saved = (self.on_learn, self.hr_max_seen, self._hr_win)
        sid_now = [None]

        def capture(kind, value, t):
            if kind == "hr_max":
                out.update(hr_max_seen=value, hr_max_seen_t=t, hr_max_session=sid_now[0])
            else:
                out["hr_rest_seen"].append(value)
                out["hr_rest_sessions"].append(sid_now[0])
        self.on_learn, self.hr_max_seen = capture, None
        try:
            for (sid,) in self.db.execute("SELECT id FROM sessions WHERE profile=? ORDER BY start",
                                          (profile,)).fetchall():
                sid_now[0] = sid
                samples, markers = self.load(sid)
                self._hr_win = None
                for smp in samples:
                    self._learn_max(smp["hr"], smp["rhythm"], smp["t"])
                if markers:
                    end = markers[1]["t"] if len(markers) > 1 else float("inf")
                    self._learn_rest([smp for smp in samples if markers[0]["t"] <= smp["t"] < end])
        finally:
            self.on_learn, self.hr_max_seen, self._hr_win = saved
        out["hr_rest_seen"] = out["hr_rest_seen"][-10:]
        return out

    def load(self, sid: int):
        con = self._con()
        samples = [{"t": r[0], "hr": r[1], "enmo_mg": r[2], "rhythm": r[3], "zone": r[4], "phase": r[5],
                    "ts_device": r[6]}
                   for r in con.execute("SELECT t,hr,enmo_mg,rhythm,zone,phase,ts_device FROM session_samples "
                                        "WHERE session_id=? ORDER BY t", (sid,))]
        markers = [{"t": r[0], "label": r[1], "kind": r[2], "source": r[3]}
                   for r in con.execute("SELECT t,label,kind,source FROM session_markers "
                                        "WHERE session_id=? ORDER BY t", (sid,))]
        return samples, markers

    def csv(self, sid: int) -> str:
        samples, markers = self.load(sid)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["unix_t", "band_ts", "elapsed_s", "phase", "hr_bpm", "zone", "enmo_mg", "rhythm_per_min"])
        t0 = samples[0]["t"] if samples else 0
        for s in samples:
            w.writerow([round(s["t"], 3), s["ts_device"], round(s["t"] - t0, 1), s["phase"], s["hr"], s["zone"],
                        s["enmo_mg"], s["rhythm"]])
        if markers:
            w.writerow([])
            w.writerow(["marker_unix_t", "elapsed_s", "label", "kind", "source"])
            for m in markers:
                w.writerow([round(m["t"], 3), round(m["t"] - t0, 1), m["label"], m["kind"], m["source"]])
        summ = self.summary(samples, markers)
        w.writerow([])
        w.writerow(["phase", "kind", "dur_s", "mean_hr", "peak_hr", "delta_hr_vs_baseline",
                    "mean_enmo_mg", "mean_rhythm", "z1_s", "z2_s", "z3_s", "z4_s", "z5_s"])
        for p in summ["phases"]:
            w.writerow([p["label"], p["kind"], p["dur_s"], p["mean_hr"], p["peak_hr"], p["delta_hr"],
                        p["mean_enmo_mg"], p["mean_rhythm"], *p["zone_s"][1:]])
        if summ["hrr"]:
            w.writerow([])
            w.writerow(["hrr_after", "hr_at_stop", "hr_60s", "hrr60_bpm"])
            for h in summ["hrr"]:
                w.writerow([h["after"], h["hr_at_stop"], h["hr_60s"], h["hrr60"]])
        return buf.getvalue()

    def imu_csv(self, sid: int):
        """100 Hz accelerometer (g) + gyroscope (raw units) from every R10 record, one row per sample.
        Sample time = record's band time (u32 seconds + u16/32768 subsecond, taken as the first
        sample) + index/FS (FS ~104 Hz); host receive time of the record kept alongside."""
        import research_playground as rp
        db = sqlite3.connect(self.db_path)   # exports stream from a worker thread
        yield "unix_t_received,band_ts,band_t_record,sample,t_est,ax_g,ay_g,az_g,gx_raw,gy_raw,gz_raw\n"
        for t, ts, data in db.execute(
                "SELECT t, ts_device, data FROM session_raw WHERE session_id=? AND packet_type=43 "
                "AND rec_type=10 ORDER BY t", (sid,)):
            d = bytes(data)
            r = rp.parse_r10(d)
            if not r:
                continue
            t_rec = int.from_bytes(d[7:11], "little") + int.from_bytes(d[11:13], "little") / 32768
            rows = []
            for i in range(len(r.accel_x)):
                rows.append(f"{t:.3f},{ts},{t_rec:.4f},{i},{t_rec + i / FS:.4f},"
                            f"{r.accel_x[i] / LSB_PER_G:.4f},{r.accel_y[i] / LSB_PER_G:.4f},"
                            f"{r.accel_z[i] / LSB_PER_G:.4f},{r.gyro_x[i]},{r.gyro_y[i]},{r.gyro_z[i]}\n")
            yield "".join(rows)

    def raw_jsonl(self, sid: int):
        """Every stored record verbatim (hex) with both timestamps, plus events — one JSON per line."""
        db = sqlite3.connect(self.db_path)   # exports stream from a worker thread
        for t, ts, pt, rt, data in db.execute(
                "SELECT t, ts_device, packet_type, rec_type, data FROM session_raw WHERE session_id=? ORDER BY t",
                (sid,)):
            yield json.dumps({"t": t, "band_ts": ts, "packet_type": pt, "rec_type": rt,
                              "hex": bytes(data).hex()}) + "\n"
        for t, kind, detail in db.execute(
                "SELECT t, kind, detail FROM session_events WHERE session_id=? ORDER BY t", (sid,)):
            yield json.dumps({"t": t, "event": kind, "detail": json.loads(detail) if detail else None}) + "\n"

    def counts(self, sid: int) -> dict:
        q = lambda sql: self.db.execute(sql, (sid,)).fetchone()[0]
        return {"samples": q("SELECT COUNT(*) FROM session_samples WHERE session_id=?"),
                "raw_records": q("SELECT COUNT(*) FROM session_raw WHERE session_id=?"),
                "imu_records": q("SELECT COUNT(*) FROM session_raw WHERE session_id=? AND packet_type=43 AND rec_type=10"),
                "markers": q("SELECT COUNT(*) FROM session_markers WHERE session_id=?"),
                "events": q("SELECT COUNT(*) FROM session_events WHERE session_id=?")}
