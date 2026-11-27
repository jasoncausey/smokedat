#!/usr/bin/env python3
"""SmokeDat: Bluetooth-to-web bridge for iBBQ-protocol thermometers (e.g. Oprol 6-probe / "BBQ 6X" app).

Keeps a BLE connection to the thermometer, logs readings, serves a live web app
(installable as a PWA), predicts time-to-goal, and sends alerts via Web Push,
Pushover (iOS Critical Alerts) and/or ntfy.
"""

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import secrets
import struct
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web
from bleak import BleakClient, BleakScanner

STATIC_DIR = Path(__file__).resolve().parent / "static"


def default_data_dir():
    """Where config, keys and logs live unless --data-dir / SMOKEDAT_DATA_DIR says otherwise."""
    if env := os.environ.get("SMOKEDAT_DATA_DIR"):
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SmokeDat"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "smokedat"


def set_data_dir(path):
    """Point every data file at `path` (called once from main, before anything reads them)."""
    global DATA_DIR, CONFIG_PATH, SUBS_PATH, VAPID_PATH, LOG_DIR, SESSION_PATH
    DATA_DIR = Path(path).expanduser().resolve()
    CONFIG_PATH = DATA_DIR / "config.json"
    SUBS_PATH = DATA_DIR / "push_subscriptions.json"
    VAPID_PATH = DATA_DIR / "vapid_private.pem"
    LOG_DIR = DATA_DIR / "logs"
    SESSION_PATH = LOG_DIR / "session.json"  # start time of the current cook session


set_data_dir(default_data_dir())

# iBBQ GATT layout
SETTINGS_RESULT = "0000fff1-0000-1000-8000-00805f9b34fb"
PAIR = "0000fff2-0000-1000-8000-00805f9b34fb"
REALTIME = "0000fff4-0000-1000-8000-00805f9b34fb"
SETTINGS = "0000fff5-0000-1000-8000-00805f9b34fb"
LOGIN = bytes.fromhex("2107060504030201b8220000000000")
ENABLE_REALTIME = bytes.fromhex("0b0100000000")
SILENCE_ALARM = bytes.fromhex("04ff00000000")  # stop the unit's beeper (0xff = all probes)
NO_PROBE = 0xFFF6

NUM_PROBES = 6
HISTORY_INTERVAL = 10  # seconds between chart/log samples
HISTORY_LEN = 24 * 3600 // HISTORY_INTERVAL  # keep 24h in memory
HYSTERESIS_C = 1.5  # ~3°F: how far back a reading must move before an alert re-arms
PUSHOVER_EXPIRE = 1800  # seconds a Pushover emergency keeps repeating if nobody acknowledges it
RECEIPT_POLL = 15  # seconds between checks for an acknowledgement made in the Pushover app
STALE_SECONDS = 60  # no data this long => "disconnected" alert
FIT_WINDOW = 60 * 60  # seconds of history used for time-to-goal prediction (60 min tested best)
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{20,40}$")
MAX_PRESETS = 50
SECRET_FIELDS = ("pushover_user", "pushover_token")

log = logging.getLogger("smokedat")


# ================================================================ config

def default_config():
    return {
        "units": "F",
        # ntfy (off by default: its iOS app currently has notification-sound bugs)
        "ntfy_enabled": False,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "bbq-" + secrets.token_hex(8),
        # Pushover: the route that can break through silent mode via iOS Critical Alerts
        "pushover_enabled": False,
        "pushover_user": "",
        "pushover_token": "",
        "pushover_emergency": True,  # temp alerts repeat until acknowledged
        # Saved alerts. Temperatures are stored in °C; the UI converts.
        "presets": [
            {"id": "brisket", "name": "Brisket", "min": None, "max": 95.0},      # 203°F
            {"id": "porkbutt", "name": "Pork butt", "min": None, "max": 95.0},   # 203°F
            {"id": "chicken", "name": "Chicken", "min": None, "max": 73.89},     # 165°F
            {"id": "smoker", "name": "Smoker 225-275", "min": 107.22, "max": 135.0},
        ],
        # Which saved alert each probe uses (preset id or None).
        "probes": [{"preset": None} for _ in range(NUM_PROBES)],
    }


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text())
            # secrets are written as-is (validate_config treats "" as "keep")
            for k in SECRET_FIELDS:
                if isinstance(stored.get(k), str):
                    cfg[k] = stored[k]
            cfg = validate_config(stored, cfg)
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("config.json invalid (%s); using defaults", e)
    save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.chmod(0o600)
    tmp.replace(CONFIG_PATH)


def public_config(cfg):
    """Config as sent to browsers: Pushover keys are never echoed back."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    out["pushover_configured"] = bool(cfg["pushover_user"] and cfg["pushover_token"])
    return out


def _temp_or_none(v):
    if v is None or v == "":
        return None
    v = float(v)
    if not math.isfinite(v) or not -50 <= v <= 500:
        raise ValueError(f"temperature {v} out of range")
    return round(v, 2)


def validate_config(data, base):
    """Merge user-supplied data onto base config, rejecting bad values. Temps are °C."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    cfg = json.loads(json.dumps(base))
    if "units" in data:
        if data["units"] not in ("F", "C"):
            raise ValueError("units must be F or C")
        cfg["units"] = data["units"]
    for flag in ("ntfy_enabled", "pushover_enabled", "pushover_emergency"):
        if flag in data:
            cfg[flag] = bool(data[flag])
    if "ntfy_server" in data:
        s = str(data["ntfy_server"]).strip().rstrip("/")
        if not s.startswith(("https://", "http://")):
            raise ValueError("ntfy server must be an http(s) URL")
        cfg["ntfy_server"] = s
    if "ntfy_topic" in data:
        t = str(data["ntfy_topic"]).strip()
        if not TOPIC_RE.match(t):
            raise ValueError("ntfy topic may only contain letters, digits, - and _")
        cfg["ntfy_topic"] = t
    for k in SECRET_FIELDS:
        if k in data:
            v = data[k]
            if v is None:  # explicit clear
                cfg[k] = ""
            elif str(v).strip():  # blank = keep existing
                v = str(v).strip()
                if not PUSHOVER_KEY_RE.match(v):
                    raise ValueError(f"{k.replace('_', ' ')} doesn't look like a Pushover key")
                cfg[k] = v
    if cfg["pushover_enabled"] and not (cfg["pushover_user"] and cfg["pushover_token"]):
        raise ValueError("enter your Pushover user key and app token first")
    if "presets" in data:
        presets = data["presets"]
        if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
            raise ValueError(f"presets must be a list of at most {MAX_PRESETS}")
        out, seen = [], set()
        for p in presets:
            pid = str(p.get("id") or secrets.token_hex(4))
            if not ID_RE.match(pid) or pid in seen:
                raise ValueError(f"bad or duplicate alert id {pid!r}")
            name = str(p.get("name", "")).strip()[:40]
            if not name:
                raise ValueError("every saved alert needs a name")
            lo, hi = _temp_or_none(p.get("min")), _temp_or_none(p.get("max"))
            if lo is None and hi is None:
                raise ValueError(f"{name}: set a low or a high temperature (or both)")
            if lo is not None and hi is not None and lo >= hi:
                raise ValueError(f"{name}: low must be below high")
            seen.add(pid)
            out.append({"id": pid, "name": name, "min": lo, "max": hi})
        cfg["presets"] = out
    if "probes" in data:
        probes = data["probes"]
        if not isinstance(probes, list) or len(probes) != NUM_PROBES:
            raise ValueError(f"need {NUM_PROBES} probes")
        cfg["probes"] = [{"preset": (p.get("preset") or None) if isinstance(p, dict) else None} for p in probes]
    # Drop assignments that point at a deleted alert.
    ids = {p["id"] for p in cfg["presets"]}
    for p in cfg["probes"]:
        if p["preset"] not in ids:
            p["preset"] = None
    return cfg


def probe_alert(cfg, i):
    """(display label, preset or None) for probe index i."""
    pid = cfg["probes"][i]["preset"]
    preset = next((p for p in cfg["presets"] if p["id"] == pid), None)
    label = f"Probe {i + 1}" + (f" ({preset['name']})" if preset else "")
    return label, preset


def fmt_temp(c, units):
    return f"{c * 9 / 5 + 32:.0f}°F" if units == "F" else f"{c:.0f}°C"


# ================================================================ prediction

# Time constants (seconds) tried for the heating-curve fit: 5 min … 20 h, log-spaced.
_TAUS = [300 * (240 ** (j / 39)) for j in range(40)]


def _linfit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    icept = my - slope * mx
    sse = sum((y - (icept + slope * x)) ** 2 for x, y in zip(xs, ys))
    return slope, icept, sse


def predict(history, i, goal):
    """Estimate seconds until probe i reaches `goal` (°C).

    Fits Newton's-law heating T(t) = Ta - B·e^(-t/tau) to the last FIT_WINDOW of
    readings (grid over tau, closed-form least squares for Ta and B), and a
    straight line. The curve is used when it fits clearly better; it captures
    the slowdown as meat approaches pit temperature. Returns a dict for the UI.
    """
    if goal is None or not history:
        return None
    now = history[-1][0]
    pts = [(t - now, temps[i]) for t, temps in history if t >= now - FIT_WINDOW and temps[i] is not None]
    if len(pts) < 30 or pts[-1][0] < -60:  # need ≥5 min of data, current within the last minute
        return None
    if pts[-1][0] - pts[0][0] < 300:
        return None
    ts, ys = [p[0] for p in pts], [p[1] for p in pts]
    if ys[-1] >= goal:
        return {"state": "reached"}

    lin = _linfit(ts, ys)
    if lin is None:
        return None
    lin_slope, lin_now, lin_sse = lin  # intercept at t=0 is the fitted "now" value

    best = None
    for tau in _TAUS:
        xs = [math.exp(-t / tau) for t in ts]  # model: y = Ta + c·x with c = -B
        f = _linfit(xs, ys)
        if f is None:
            continue
        c, ta, sse = f
        if c < 0 and (best is None or sse < best[3]):  # B > 0: approaching Ta from below
            best = (tau, ta, -c, sse)

    if best and best[3] < 0.85 * lin_sse:
        tau, ta, b, _ = best
        rate = b / tau * 60  # °C/min at t=0
        model, plateau = "curve", ta
        fitted_now = ta - b
        if ta <= goal + 0.5:
            return {"state": "stalled", "rate": rate, "plateau": ta, "model": model}
        eta = tau * math.log(b / (ta - goal)) if ta - goal < b else 0.0
    else:
        rate = lin_slope * 60
        model, plateau = "linear", None
        fitted_now = lin_now
        eta = (goal - fitted_now) / lin_slope if lin_slope > 0 else None

    if rate < 0.05:  # < ~0.1°F/min: flat or falling
        return {"state": "stalled" if rate > -0.05 else "falling", "rate": rate, "plateau": plateau, "model": model}
    if eta is None or eta > 24 * 3600:
        return {"state": "stalled", "rate": rate, "plateau": plateau, "model": model}
    return {"state": "rising", "eta": max(0.0, eta), "rate": rate, "plateau": plateau, "model": model}


# ================================================================ state

class State:
    def __init__(self):
        self.temps = [None] * NUM_PROBES  # °C, None = no probe
        self.last_data = None
        self.ever_connected = False
        self.connected = False
        self.status = "starting"
        self.history = deque(maxlen=HISTORY_LEN)  # (epoch, [temps])
        self.predictions = [None] * NUM_PROBES
        self.alerts = deque(maxlen=50)  # dicts
        self.alert_seq = 0
        self.session_start = time.time()  # charts, predictions and export cover readings from here on

    def open_alarms(self):
        return [a for a in self.alerts if a.get("alarm") and not a.get("acked")]


# ================================================================ notifications

class Notifier:
    """Fans an alert out to every enabled channel. Levels: 'alarm', 'warn', 'info'."""

    def __init__(self, app, vapid_sub):
        self.app = app
        self.vapid_sub = vapid_sub
        self.subs = []
        if SUBS_PATH.exists():
            try:
                self.subs = json.loads(SUBS_PATH.read_text())
            except json.JSONDecodeError:
                log.warning("push_subscriptions.json unreadable; starting empty")
        self.vapid_public = _load_vapid_public()
        self.receipts = {}  # Pushover emergency receipt -> sent time; these repeat until acknowledged

    async def cancel_receipts(self):
        """Stop every outstanding Pushover emergency from repeating."""
        token = self.app["config"]["pushover_token"]
        receipts, self.receipts = list(self.receipts), {}
        for rc in receipts:
            try:
                async with self.app["http"].post(f"https://api.pushover.net/1/receipts/{rc}/cancel.json",
                                                 data={"token": token},
                                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status >= 300:
                        log.warning("pushover cancel %s: HTTP %s", rc, r.status)
            except Exception as e:
                log.warning("pushover cancel %s failed: %s", rc, e)

    async def acknowledged_in_pushover(self):
        """Poll outstanding receipts. Return True if any was acknowledged in the Pushover app."""
        token = self.app["config"]["pushover_token"]
        acked = False
        for rc, sent in list(self.receipts.items()):
            if time.time() - sent > PUSHOVER_EXPIRE + 60:
                self.receipts.pop(rc, None)
                continue
            try:
                async with self.app["http"].get(f"https://api.pushover.net/1/receipts/{rc}.json",
                                                params={"token": token},
                                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                    j = await r.json(content_type=None)
            except Exception as e:
                log.debug("pushover receipt %s poll failed: %s", rc, e)
                continue
            if j.get("acknowledged") == 1:
                acked = True
            if j.get("acknowledged") == 1 or j.get("expired") == 1 or r.status == 404:
                self.receipts.pop(rc, None)
        return acked

    def _save_subs(self):
        SUBS_PATH.write_text(json.dumps(self.subs, indent=2))
        SUBS_PATH.chmod(0o600)

    def add_sub(self, sub, ua=""):
        if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") \
                or not isinstance(sub.get("keys"), dict):
            raise ValueError("invalid push subscription")
        self.subs = [s for s in self.subs if s["endpoint"] != sub["endpoint"]]
        self.subs.append({"endpoint": sub["endpoint"], "keys": sub["keys"],
                          "ua": str(ua)[:200], "added": time.time()})
        self._save_subs()

    def remove_sub(self, endpoint):
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint]
        self._save_subs()

    async def send(self, title, message, level="info"):
        cfg = self.app["config"]
        jobs, names = [], []
        if self.subs:
            jobs.append(self._webpush_all(title, message, level)); names.append("web push")
        if cfg["pushover_enabled"]:
            jobs.append(self._pushover(title, message, level)); names.append("pushover")
        if cfg["ntfy_enabled"]:
            jobs.append(self._ntfy(title, message, level)); names.append("ntfy")
        results = await asyncio.gather(*jobs, return_exceptions=True)
        out = {}
        for n, r in zip(names, results):
            out[n] = r if isinstance(r, str) else f"error: {r}"
            if isinstance(r, Exception):
                log.warning("%s failed: %s", n, r)
        return out

    async def _ntfy(self, title, message, level):
        cfg = self.app["config"]
        body = {"topic": cfg["ntfy_topic"], "title": title, "message": message,
                "priority": {"alarm": 5, "warn": 4}.get(level, 3),
                "tags": {"alarm": ["fire"], "warn": ["warning"]}.get(level, ["meat_on_bone"])}
        async with self.app["http"].post(cfg["ntfy_server"] + "/", json=body,
                                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status >= 300:
                raise RuntimeError(f"ntfy HTTP {r.status}")
        return "sent"

    async def _pushover(self, title, message, level):
        cfg = self.app["config"]
        prio = {"alarm": 2 if cfg["pushover_emergency"] else 1, "warn": 1}.get(level, 0)
        form = {"token": cfg["pushover_token"], "user": cfg["pushover_user"],
                "title": title, "message": message, "priority": str(prio)}
        if prio == 2:
            # re-alert every minute until acknowledged (in Pushover, or anywhere via Alerter.acknowledge)
            form.update(retry="60", expire=str(PUSHOVER_EXPIRE))
        async with self.app["http"].post("https://api.pushover.net/1/messages.json", data=form,
                                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            j = await r.json(content_type=None)
            if r.status >= 300:
                raise RuntimeError("; ".join(j.get("errors", [])) or f"HTTP {r.status}")
        if j.get("receipt"):
            self.receipts[j["receipt"]] = time.time()
        return "sent"

    async def _webpush_all(self, title, message, level):
        from pywebpush import WebPushException, webpush

        payload = json.dumps({"title": title, "body": message, "level": level,
                              "tag": "bbq-" + level if level != "alarm" else f"bbq-alarm-{time.time():.0f}"})
        headers = {"Urgency": "high" if level in ("alarm", "warn") else "normal"}

        def push(sub):
            try:
                webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=payload,
                        vapid_private_key=str(VAPID_PATH), vapid_claims={"sub": self.vapid_sub},
                        ttl=3600, headers=headers, timeout=10)
                return "ok"
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                return "gone" if code in (404, 410) else f"HTTP {code}: {e}"

        results = await asyncio.gather(*(asyncio.to_thread(push, s) for s in list(self.subs)))
        for sub, r in zip(list(self.subs), results):
            if r == "gone":
                log.info("removing expired push subscription")
                self.remove_sub(sub["endpoint"])
        ok = sum(r == "ok" for r in results)
        errs = [r for r in results if r not in ("ok", "gone")]
        if errs and not ok:
            raise RuntimeError(errs[0])
        return f"sent to {ok} device(s)" + (f", {len(errs)} failed" if errs else "")


def _load_vapid_public():
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    from py_vapid.utils import b64urlencode

    if not VAPID_PATH.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(VAPID_PATH))
        VAPID_PATH.chmod(0o600)
        log.info("generated web push keys")
    v = Vapid.from_file(str(VAPID_PATH))
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


# ================================================================ alerts

class Alerter:
    def __init__(self, app):
        self.app = app
        # (kind, probe) -> [threshold, armed, fired_before]
        self.arm = {}
        self.conn_alerted = False

    def _slot(self, key, value, armed_initially):
        slot = self.arm.get(key)
        if slot is None or slot[0] != value:  # new or changed threshold resets arming
            slot = [value, armed_initially, False]
            self.arm[key] = slot
        return slot

    async def check(self):
        cfg, st = self.app["config"], self.app["state"]
        u = cfg["units"]
        for i in range(NUM_PROBES):
            t = st.temps[i]
            label, p = probe_alert(cfg, i)
            if t is None or p is None:
                continue

            hi = p.get("max")
            if hi is not None:
                s = self._slot(("max", i), hi, True)
                if s[1] and t >= hi:
                    s[1], s[2] = False, True
                    await self.fire(f"{label}: high alert",
                                    f"{label} is at {fmt_temp(t, u)} (high alert {fmt_temp(hi, u)})", "alarm")
                elif not s[1] and t < hi - HYSTERESIS_C:
                    s[1] = True

            lo = p.get("min")
            if lo is not None:
                # Low alerts only arm once the probe has climbed to the minimum,
                # so a cold grill at startup doesn't immediately alarm.
                s = self._slot(("min", i), lo, False)
                if s[1] and t < lo:
                    s[1], s[2] = False, True
                    await self.fire(f"{label}: low alert",
                                    f"{label} dropped to {fmt_temp(t, u)} (low alert {fmt_temp(lo, u)})", "alarm")
                elif not s[1] and t >= lo + (HYSTERESIS_C if s[2] else 0):
                    s[1] = True

    async def check_connection(self):
        st = self.app["state"]
        if not st.ever_connected:
            return
        stale = st.last_data is None or time.time() - st.last_data > STALE_SECONDS
        if stale and not self.conn_alerted:
            self.conn_alerted = True
            await self.fire("Thermometer disconnected",
                            f"No readings for {STALE_SECONDS}s. The bridge keeps retrying. Check the thermometer "
                            "is on and in range, and that the phone app isn't holding the connection.", "warn")
        elif not stale and self.conn_alerted:
            self.conn_alerted = False
            await self.fire("Thermometer reconnected", "Readings are coming in again.", "info")

    async def fire(self, title, message, level):
        st = self.app["state"]
        log.info("ALERT %s: %s", title, message)
        st.alert_seq += 1
        entry = {"id": st.alert_seq, "t": time.time(), "title": title, "message": message, "delivery": {}}
        if level == "alarm":
            entry.update(alarm=True, acked=None)  # acked becomes {"t", "from"}
        st.alerts.appendleft(entry)
        entry["delivery"] = await self.app["notifier"].send(title, message, level)

    async def silence_unit(self):
        """Stop the thermometer's own beeper. It beeps on targets set in the BBQ 6X app."""
        client = self.app["ble"]["client"]
        if client is None or not client.is_connected:
            return False
        try:
            await client.write_gatt_char(SETTINGS, SILENCE_ALARM, response=True)
            log.info("sent silence command to thermometer")
            return True
        except Exception as e:
            log.warning("could not silence thermometer: %s", e)
            return False

    async def acknowledge(self, where):
        """Silence every outstanding alarm. Later alarms (e.g. from another probe) still notify."""
        st, n = self.app["state"], self.app["notifier"]
        await self.silence_unit()  # always: the unit may beep on its own targets with no bridge alarm
        open_ = st.open_alarms()
        if not open_ and not n.receipts:
            return False
        log.info("alarms acknowledged (%s)", where)
        for a in open_:
            a["acked"] = {"t": time.time(), "from": where}
        await n.cancel_receipts()
        return True


# ================================================================ bluetooth

def _matches(target):
    target_l = target.lower()

    def f(dev, adv):
        name = adv.local_name or dev.name or ""
        return dev.address.lower() == target_l or name.lower() == target_l
    return f


async def ble_loop(app, device):
    st = app["state"]

    def on_realtime(_, data: bytearray):
        vals = struct.unpack("<" + "H" * (len(data) // 2), data[: len(data) // 2 * 2])
        temps = [None if v == NO_PROBE else v / 10 for v in vals[:NUM_PROBES]]
        temps += [None] * (NUM_PROBES - len(temps))
        st.temps = temps
        st.last_data = time.time()

    def on_settings(_, data: bytearray):
        log.debug("settings reply %s", data.hex())

    while True:
        try:
            st.status = "searching for thermometer"
            dev = await BleakScanner.find_device_by_filter(_matches(device), timeout=20)
            if dev is None:
                st.status = "thermometer not found (off, out of range, or the phone app is connected)"
                await asyncio.sleep(5)
                continue
            st.status = "connecting"
            disconnected = asyncio.Event()
            async with BleakClient(dev, disconnected_callback=lambda _c: disconnected.set()) as client:
                await client.start_notify(SETTINGS_RESULT, on_settings)
                await client.write_gatt_char(PAIR, LOGIN, response=True)
                await client.start_notify(REALTIME, on_realtime)
                await client.write_gatt_char(SETTINGS, ENABLE_REALTIME, response=True)
                app["ble"]["client"] = client
                st.connected = st.ever_connected = True
                st.status = "connected"
                log.info("connected to %s", dev.address)
                while not disconnected.is_set():
                    try:
                        await asyncio.wait_for(disconnected.wait(), 15)
                    except TimeoutError:
                        if st.last_data is None or time.time() - st.last_data > 30:
                            log.warning("no data for 30s; reconnecting")
                            break
            log.info("disconnected")
        except Exception as e:
            log.warning("bluetooth error: %s", e)
            st.status = f"bluetooth error: {e}"
        finally:
            st.connected = False
            app["ble"]["client"] = None
        await asyncio.sleep(3)


# ================================================================ logging / background

def load_session_start():
    try:
        return float(json.loads(SESSION_PATH.read_text())["start"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_session_start(t):
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"start": t}))
    tmp.replace(SESSION_PATH)


def read_log_rows(since):
    """All logged samples with epoch >= since, across every CSV (one per bridge run), sorted."""
    rows = {}
    for path in sorted(LOG_DIR.glob("cook-*.csv")):
        if path.stat().st_mtime < since:
            continue
        try:
            with path.open(newline="") as fh:
                for r in csv.DictReader(fh):
                    t = float(r["epoch"])
                    if t >= since:
                        rows[t] = [float(r[f"probe{i + 1}_C"]) if r.get(f"probe{i + 1}_C") else None
                                   for i in range(NUM_PROBES)]
        except (OSError, KeyError, ValueError) as e:
            log.warning("skipping %s: %s", path.name, e)
    return sorted(rows.items())


def load_recent_history(st):
    """Reload the current session's last 24h from CSV logs so charts survive restarts."""
    rows = read_log_rows(max(time.time() - HISTORY_LEN * HISTORY_INTERVAL, st.session_start))
    st.history.extend(rows)
    if rows:
        log.info("loaded %d earlier samples from logs", len(rows))


class CookLog:
    """The CSV file readings are currently written to. A new session starts a new file."""

    HEADER = ["epoch", "time"] + [f"probe{i + 1}_C" for i in range(NUM_PROBES)]

    def __init__(self):
        self.fh = self.writer = self.path = None

    def start(self):
        self.close()
        LOG_DIR.mkdir(exist_ok=True)
        stem = f"cook-{datetime.now():%Y%m%d-%H%M%S}"
        path, n = LOG_DIR / f"{stem}.csv", 2
        while path.exists():
            path, n = LOG_DIR / f"{stem}-{n}.csv", n + 1
        self.path = path
        self.fh = path.open("w", newline="")
        self.writer = csv.writer(self.fh)
        self.writer.writerow(self.HEADER)
        self.fh.flush()
        log.info("logging readings to %s", path)

    def write(self, now, temps):
        self.writer.writerow([round(now), datetime.fromtimestamp(now).isoformat(timespec="seconds")]
                             + ["" if t is None else t for t in temps])
        self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


async def receipt_watcher(app):
    """Acknowledging one Pushover alarm in the Pushover app silences the rest too."""
    n = app["notifier"]
    while True:
        await asyncio.sleep(RECEIPT_POLL)
        try:
            if n.receipts and await n.acknowledged_in_pushover():
                await app["alerter"].acknowledge("Pushover")
        except Exception:
            log.exception("receipt watcher error")


async def sampler(app):
    """Record history + CSV every HISTORY_INTERVAL, refresh predictions, evaluate alerts.

    Samples are only recorded while fresh data is arriving, so a dropped
    connection simply leaves a gap in the log and charts.
    """
    st, alerter, cooklog = app["state"], app["alerter"], app["cooklog"]
    last_sample = 0.0
    try:
        while True:
            try:
                now = time.time()
                fresh = st.last_data is not None and now - st.last_data < 15
                if fresh:
                    await alerter.check()
                    if now - last_sample >= HISTORY_INTERVAL:
                        last_sample = now
                        st.history.append((round(now, 1), list(st.temps)))
                        cooklog.write(now, st.temps)
                        cfg = app["config"]
                        st.predictions = [predict(st.history, i, (probe_alert(cfg, i)[1] or {}).get("max"))
                                          for i in range(NUM_PROBES)]
                else:
                    st.predictions = [None] * NUM_PROBES
                await alerter.check_connection()
            except Exception:
                log.exception("sampler error")
            await asyncio.sleep(2)
    finally:
        cooklog.close()


# ================================================================ web

def static(name, content_type=None, cache="no-cache"):
    async def handler(_request):
        resp = web.FileResponse(STATIC_DIR / name, headers={"Cache-Control": cache})
        if content_type:
            resp.content_type = content_type
        return resp
    return handler


async def api_state(request):
    st = request.app["state"]
    try:
        since = float(request.query.get("since", 0))
    except ValueError:
        since = 0
    return web.json_response({
        "now": time.time(),
        "connected": st.connected,
        "status": st.status,
        "last_data": st.last_data,
        "temps": st.temps,
        "predictions": st.predictions,
        "history": [[t, temps] for t, temps in st.history if t > since],
        "alerts": list(st.alerts)[:15],
        "active_alarms": st.open_alarms(),
        "config": public_config(request.app["config"]),
        "push_devices": len(request.app["notifier"].subs),
        "session": {"start": st.session_start, "file": request.app["cooklog"].path.name},
    })


async def api_export(request):
    """The current session as a CSV download, in the requested (or configured) units."""
    cfg, st = request.app["config"], request.app["state"]
    units = request.query.get("units", cfg["units"])
    if units not in ("F", "C"):
        units = cfg["units"]
    start = st.session_start
    rows = await asyncio.to_thread(read_log_rows, start)
    used = [i for i in range(NUM_PROBES) if any(r[1][i] is not None for r in rows)]
    conv = (lambda c: round(c * 9 / 5 + 32, 1)) if units == "F" else (lambda c: c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "elapsed_min", "epoch"] + [f"probe{i + 1}_{units}" for i in used])
    for t, temps in rows:
        w.writerow([datetime.fromtimestamp(t).isoformat(sep=" ", timespec="seconds"),
                    round((t - start) / 60, 2), round(t)]
                   + ["" if temps[i] is None else conv(temps[i]) for i in used])
    name = f"smokedat-{datetime.fromtimestamp(start):%Y%m%d-%H%M}.csv"
    return web.Response(text=buf.getvalue(), content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "no-store"})


async def api_session_reset(request):
    """Start a new cook: fresh chart, predictions, alert list and log file. Old CSVs stay on disk."""
    app, st = request.app, request.app["state"]
    now = time.time()
    app["cooklog"].start()
    st.session_start = now
    save_session_start(now)
    st.history.clear()
    st.predictions = [None] * NUM_PROBES
    still_open = st.open_alarms()  # unacknowledged alarms stay so they can still be silenced
    st.alerts.clear()
    st.alerts.extend(still_open)
    log.info("new session started")
    return web.json_response({"ok": True, "session": {"start": now, "file": app["cooklog"].path.name}})


async def api_config(request):
    try:
        data = await request.json()
        cfg = validate_config(data, request.app["config"])
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    live = request.app["config"]  # update in place: aiohttp forbids reassigning app state after startup
    live.clear()
    live.update(cfg)
    save_config(live)
    return web.json_response(public_config(cfg))


async def api_test(request):
    results = await request.app["notifier"].send(
        "SmokeDat test", "If you can read this, alerts will reach this device.", "info")
    if not results:
        return web.json_response({"error": "no notification channels are set up yet"}, status=400)
    return web.json_response({"results": results})


async def api_ack(request):
    try:
        where = str((await request.json()).get("from", "the web app"))[:40]
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        where = "the web app"
    return web.json_response({"ok": True, "silenced": await request.app["alerter"].acknowledge(where)})


async def api_push_key(request):
    return web.json_response({"key": request.app["notifier"].vapid_public})


async def api_push_subscribe(request):
    try:
        data = await request.json()
        request.app["notifier"].add_sub(data.get("subscription"), request.headers.get("User-Agent", ""))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "devices": len(request.app["notifier"].subs)})


async def api_push_unsubscribe(request):
    try:
        data = await request.json()
        request.app["notifier"].remove_sub(str(data.get("endpoint", "")))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True})


async def on_startup(app):
    st = app["state"]
    LOG_DIR.mkdir(exist_ok=True)
    start = load_session_start()
    st.session_start = start or 0
    load_recent_history(st)
    if start is None:  # first run with sessions: the session is whatever the chart already shows
        start = st.history[0][0] if st.history else time.time()
        save_session_start(start)
    st.session_start = start
    app["cooklog"].start()
    app["http"] = aiohttp.ClientSession()
    app["tasks"] = [asyncio.create_task(ble_loop(app, app["device"])),
                    asyncio.create_task(sampler(app)),
                    asyncio.create_task(receipt_watcher(app))]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    await asyncio.gather(*app["tasks"], return_exceptions=True)
    await app["http"].close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to serve on (default 127.0.0.1; use `tailscale serve` for HTTPS access)")
    ap.add_argument("--port", type=int, default=9339)
    ap.add_argument("--device", default="iBBQ", help="BLE name or address of the thermometer")
    ap.add_argument("--vapid-sub", default="mailto:bbq-bridge@example.com",
                    help="contact claim sent to push services with web push requests")
    ap.add_argument("--data-dir", type=Path, default=default_data_dir(),
                    help="where config.json, push keys and logs/ are kept "
                         "(default: %(default)s; or set SMOKEDAT_DATA_DIR)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    set_data_dir(args.data_dir)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("data directory: %s", DATA_DIR)

    app = web.Application(client_max_size=64 * 1024)
    app["config"] = load_config()
    app["ble"] = {"client": None}  # mutable: aiohttp warns on reassigning app state after startup
    app["state"] = State()
    app["alerter"] = Alerter(app)
    app["notifier"] = Notifier(app, args.vapid_sub)
    app["device"] = args.device
    app["cooklog"] = CookLog()
    app.router.add_get("/", static("index.html"))
    app.router.add_get("/sw.js", static("sw.js", "application/javascript"))
    app.router.add_get("/manifest.webmanifest", static("manifest.webmanifest", "application/manifest+json"))
    app.router.add_get("/icon.svg", static("icon.svg", "image/svg+xml", cache="max-age=86400"))
    app.router.add_get("/favicon.svg", static("favicon.svg", "image/svg+xml", cache="max-age=86400"))
    for icon in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        app.router.add_get("/" + icon, static(icon, cache="max-age=86400"))
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/test", api_test)
    app.router.add_post("/api/ack", api_ack)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/session/reset", api_session_reset)
    app.router.add_get("/api/push/key", api_push_key)
    app.router.add_post("/api/push/subscribe", api_push_subscribe)
    app.router.add_post("/api/push/unsubscribe", api_push_unsubscribe)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=args.host, port=args.port,
                print=lambda *_: log.info("serving on http://%s:%s", args.host, args.port))


if __name__ == "__main__":
    main()
