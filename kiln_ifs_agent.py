# /// script
# [tool.orcaslicer.plugin]
# name = "Kiln OS IFS Agent"
# description = "Printer agent for Flashforge AD5X on Kiln OS: syncs the IFS slots and applies the filament-to-slot choice made in the Send dialog."
# author = "zib0"
# version = "0.4.0"
# license = "GPL-3.0-or-later"
# ///
"""
Kiln OS IFS Agent - OrcaSlicer printer agent for the Flashforge AD5X on Kiln OS.

Copyright (C) 2026 zib0

What it does
  1. Shows the four IFS slots in Orca's Device tab as an AMS unit (A1..A4),
     with the loaded slot, and lets you edit each slot's material and colour.
  2. Uploads the sliced G-code through Moonraker.
  3. Applies the filament -> slot choice from the "Send print job" dialog with
     IFS_MAP_TOOL TOOL=n SLOT=m, then starts the print.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file or
<https://www.gnu.org/licenses/> for details.
"""
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
import zipfile

import orca

pa = orca.printer_agent
log = logging.getLogger("kiln_ifs_agent")

AGENT_ID = "kiln_ifs_agent"
VERSION = "0.4.0"
PRINTER_NAME = "Kiln OS AD5X"
POLL_SECONDS = 10
HTTP_TIMEOUT = 8

# Settings shown in the plugin's Config tab (Plugins dialog).
DEFAULT_CONFIG = {
    "printer_ip": "",  # e.g. 192.168.1.1 (the same address that opens Fluidd)
    "port": "",        # empty = 80
}

# Progress stages as used by the Bambu network layer (best guess, adjust if
# the progress bar misbehaves).
ST_UPLOAD = 1
ST_SEND = 3
ST_DONE = 6
ST_ERROR = 7

def normalize_host(value):
    """'http://192.168.1.1:7125/x' -> ('192.168.1.1', '7125')."""
    v = re.sub(r"^[A-Za-z]+://", "", (value or "").strip())
    v = v.split("/")[0]
    host, _, port = v.partition(":")
    return host.strip(), port.strip()


CONFIG_UI = r"""
<style>
  body { font-family: var(--orca-font, sans-serif); padding: 16px; max-width: 520px; }
  h3 { margin: 0 0 4px 0; }
  p.hint { color: var(--orca-muted, #888); margin: 2px 0 12px 0; font-size: 12px; }
  label { display: block; font-weight: 600; margin-top: 12px; }
  input { width: 100%; box-sizing: border-box; padding: 6px 8px; margin-top: 4px; }
  button { margin-top: 18px; padding: 7px 18px; }
  #msg { margin-left: 12px; }
  .err { color: #d33; } .ok { color: #2a9d4b; }
  p.credits { color: var(--orca-muted, #888); font-size: 11px; margin-top: 28px;
              border-top: 1px solid rgba(128,128,128,.25); padding-top: 10px; }
</style>
<h3>Kiln OS (IFS)</h3>
<p class="hint">Address of your AD5X. It is the same one that opens Fluidd in the browser.</p>
<label>Printer IP or address
  <input id="ip" placeholder="192.168.1.1" autocomplete="off">
</label>
<p class="hint">Leave empty to use the print host of the printer preset.</p>
<label>Port (optional)
  <input id="port" placeholder="80">
</label>
<button id="save">Save</button><span id="msg"></span>
<p class="credits">Kiln OS IFS Agent v0.4.0 &middot; by zib0<br>
Licensed under the GNU GPL v3 or later.</p>
<script>
  const $ = id => document.getElementById(id);
  function parse(c) {
    if (typeof c === 'string') { try { c = JSON.parse(c); } catch (e) { c = {}; } }
    return c || {};
  }
  function fill(c) {
    $('ip').value = c.printer_ip || '';
    $('port').value = c.port || '';
  }
  Promise.resolve(window.orca.getConfig()).then(c => fill(parse(c)));
  function show(text, cls) { const m = $('msg'); m.textContent = text; m.className = cls; }
  $('save').onclick = () => {
    let ip = $('ip').value.trim().replace(/^[a-zA-Z]+:\/\//, '').split('/')[0];
    let port = $('port').value.trim();
    if (ip.indexOf(':') > -1) { const p = ip.split(':'); ip = p[0]; if (!port) port = p[1]; }
    if (ip && !/^[A-Za-z0-9._-]+$/.test(ip)) { show('Invalid address', 'err'); return; }
    if (port && !/^[0-9]{1,5}$/.test(port)) { show('Invalid port', 'err'); return; }
    const cfg = { printer_ip: ip, port: port };
    fill(cfg);
    Promise.resolve(window.orca.saveConfig(cfg)).then(
      () => show('Saved. Reselect the printer or restart Orca to apply.', 'ok'),
      () => show('Could not save', 'err'));
  };
</script>
"""


# --------------------------------------------------------------------------
# Moonraker client (stdlib only)
# --------------------------------------------------------------------------
class Moonraker:
    def __init__(self, host, port):
        port = str(port or "").strip()
        self.base = "http://%s%s" % (host, (":" + port) if port and port != "80" else "")

    def _open(self, req, timeout=HTTP_TIMEOUT):
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}

    def get(self, path):
        return self._open(urllib.request.Request(self.base + path))

    def post_json(self, path, data):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open(req, timeout=30)

    def gcode(self, script, timeout=30):
        req = urllib.request.Request(
            self.base + "/printer/gcode/script",
            data=json.dumps({"script": script}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open(req, timeout=timeout)

    def lane_data(self):
        j = self.get("/server/database/item?namespace=lane_data")
        return (j.get("result") or {}).get("value") or {}

    def upload(self, name, data):
        boundary = "----kiln" + uuid.uuid4().hex
        parts = []

        def field(k, v):
            parts.append(
                ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
            )

        field("root", "gcodes")
        field("print", "false")
        parts.append(
            (
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)
            ).encode()
        )
        parts.append(data)
        parts.append(("\r\n--%s--\r\n" % boundary).encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/server/files/upload",
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            method="POST",
        )
        return self._open(req, timeout=300)

    def start_print(self, filename):
        return self.post_json("/printer/print/start", {"filename": filename})

    def live(self):
        """Temperatures, print state, progress, fan and speed from Klipper."""
        q = ("/printer/objects/query?extruder&heater_bed&print_stats&display_status&virtual_sdcard"
             "&fan&fan_generic%20fanM106&gcode_move&ifs&ifs_materials&led%20chamber_led"
             "&filament_switch_sensor%20toolhead")
        return (self.get(q).get("result") or {}).get("status") or {}

    def control(self, what):
        return self.post_json("/printer/print/" + what, {})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "print").strip("._") or "print"
    if name.lower().endswith(".gcode"):
        name = name[:-6]
    return name + ".gcode"


def read_gcode(path, plate_index):
    """Return the G-code bytes of `path`; unpack a sliced .gcode.3mf if needed."""
    if not zipfile.is_zipfile(path):
        with open(path, "rb") as f:
            return f.read()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        plates = sorted(n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
        if not plates:
            raise RuntimeError("no sliced G-code inside the 3MF")
        for cand in ("Metadata/plate_%d.gcode" % plate_index, "Metadata/plate_%d.gcode" % (plate_index + 1)):
            if cand in names:
                return z.read(cand)
        return z.read(plates[0])


def parse_json(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def mapping_from_params(params):
    """Return {tool_index: slot_number(1..4)} from the Send dialog's mapping."""
    result = {}
    v1 = parse_json(getattr(params, "ams_mapping2", ""))
    if isinstance(v1, list):
        for tool, item in enumerate(v1):
            try:
                ams_id = int(item.get("ams_id", -1))
                slot_id = int(item.get("slot_id", -1))
            except Exception:
                continue
            if ams_id == 0 and 0 <= slot_id <= 3:
                result[tool] = slot_id + 1
        if result:
            return result
    v0 = parse_json(getattr(params, "ams_mapping", ""))
    if isinstance(v0, list):
        for tool, tray in enumerate(v0):
            try:
                tray = int(tray)
            except Exception:
                continue
            if 0 <= tray <= 3:
                result[tool] = tray + 1
    return result


def _slot_from(p):
    """Slot number 1..4 from a Bambu command (ams_id + tray_id/slot_id)."""
    try:
        if int(p.get("ams_id", 0)) != 0:
            return None
        slot = int(p.get("slot_id", p.get("tray_id", -1)))
    except Exception:
        return None
    return slot + 1 if 0 <= slot <= 3 else None


def filament_setting_gcode(p):
    """ams_filament_setting -> IFS_SET_MATERIAL SLOT= TYPE= COLOR= TEMP=."""
    slot = _slot_from(p)
    if slot is None:
        return None
    mat = re.sub(r"[^A-Za-z0-9+_-]+", "-", str(p.get("tray_type") or "")).strip("-")
    col = re.sub(r"[^0-9A-Fa-f]", "", str(p.get("tray_color") or ""))[:6].upper()
    if mat and len(col) != 6:
        col = "FFFFFF"
    try:
        tmin = int(float(p.get("nozzle_temp_min") or 0))
        tmax = int(float(p.get("nozzle_temp_max") or 0))
        temp = (tmin + tmax) // 2 if tmin and tmax else (tmax or tmin)
    except Exception:
        temp = 0
    line = "IFS_SET_MATERIAL SLOT=%d TYPE=%s COLOR=%s" % (slot, mat, col if mat else "")
    if mat and temp > 0:
        line += " TEMP=%d" % temp
    return line


def change_filament_gcode(p):
    """ams_change_filament -> IFS_LOAD SLOT= / IFS_UNLOAD."""
    try:
        target = int(p.get("target", -1))
    except Exception:
        target = -1
    try:
        temp = int(float(p.get("tar_temp") or 0))
    except Exception:
        temp = 0
    if target == 255:
        return "IFS_UNLOAD"
    slot = _slot_from(p) if "slot_id" in p else (target + 1 if 0 <= target <= 3 else None)
    if slot is None:
        return None
    return "IFS_LOAD SLOT=%d" % slot + (" TEMP=%d" % temp if temp > 0 else "")


STATE_MAP = {"standby": "IDLE", "printing": "RUNNING", "paused": "PAUSE",
             "complete": "FINISH", "error": "FAILED", "cancelled": "IDLE"}


def live_fields(live):
    """Map Klipper objects to the Bambu-style fields the Device tab reads."""
    out = {}
    if not live:
        return out
    ex = live.get("extruder") or {}
    bed = live.get("heater_bed") or {}
    ps = live.get("print_stats") or {}
    ds = live.get("display_status") or {}
    vs = live.get("virtual_sdcard") or {}
    fan = live.get("fan_generic fanM106") or live.get("fan") or {}
    gm = live.get("gcode_move") or {}

    def num(v, d=0.0):
        try:
            return float(v)
        except Exception:
            return d

    out["nozzle_temper"] = round(num(ex.get("temperature")), 1)
    out["nozzle_target_temper"] = round(num(ex.get("target")), 1)
    out["bed_temper"] = round(num(bed.get("temperature")), 1)
    out["bed_target_temper"] = round(num(bed.get("target")), 1)
    out["chamber_temper"] = 0
    state = ps.get("state") or "standby"
    out["gcode_state"] = STATE_MAP.get(state, "IDLE")
    progress = num(vs.get("progress"), num(ds.get("progress")))
    out["mc_percent"] = int(round(progress * 100))
    dur = num(ps.get("print_duration"))
    if state in ("printing", "paused") and progress > 0.01 and dur > 0:
        out["mc_remaining_time"] = int(max(dur / progress - dur, 0) / 60)
    else:
        out["mc_remaining_time"] = 0
    name = ps.get("filename") or ""
    out["subtask_name"] = os.path.splitext(name)[0]
    out["gcode_file"] = name
    info = ps.get("info") or {}
    out["layer_num"] = int(num(info.get("current_layer")))
    out["total_layer_num"] = int(num(info.get("total_layer")))
    fan_level = str(int(round(num(fan.get("speed")) * 15)))
    out["cooling_fan_speed"] = fan_level
    out["big_fan1_speed"] = "0"
    out["big_fan2_speed"] = "0"
    out["heatbreak_fan_speed"] = "0"
    factor = num(gm.get("speed_factor"), 1.0)
    out["spd_mag"] = int(round(factor * 100))
    out["spd_lvl"] = 1 if factor < 0.75 else 2 if factor < 1.1 else 3 if factor < 1.45 else 4
    out["wifi_signal"] = "-50dBm"
    led = (live.get("led chamber_led") or {}).get("color_data") or [[0, 0, 0, 0]]
    lit = any(num(c) > 0 for c in (led[0] if led else []))
    out["lights_report"] = [{"node": "chamber_light", "mode": "on" if lit else "off"}]
    return out


def lanes_from_live(live):
    """IFS slots from Klipper's ifs_materials object, in the lane_data shape.

    Returns None when the object is missing, so the caller can fall back to
    the Moonraker database."""
    mats = (live or {}).get("ifs_materials") or {}
    slots = mats.get("slots")
    if not isinstance(slots, dict):
        return None
    lanes = {}
    for n in range(1, 5):
        s = slots.get(str(n)) or {}
        lanes["lane%d" % n] = {"material": s.get("type") or "", "color": s.get("color") or "",
                               "nozzle_temp": s.get("temp") or 0}
    return lanes


def ifs_state(live):
    """(loaded slot index 0..3 or None, auto refill on?) from the ifs object."""
    ifs = (live or {}).get("ifs") or {}
    sensor = (live or {}).get("filament_switch_sensor toolhead") or {}
    loaded = None
    try:
        ch = int(ifs.get("channel") or 0)
        if 1 <= ch <= 4 and sensor.get("filament_detected", True):
            loaded = ch - 1
    except Exception:
        pass
    return loaded, bool(ifs.get("auto_refill"))


def build_version_info(dev_id):
    """Answer to the 'get_version' request Orca sends when it connects."""
    return {"info": {"command": "get_version", "sequence_id": "0", "result": "success", "reason": "",
                     "module": [{"name": "ota", "project_name": "", "sw_ver": "01.00.00.00",
                                 "hw_ver": "AD5X", "sn": dev_id, "product_name": "Kiln OS AD5X"}]}}


def build_status(lanes, live=None):
    """Bambu-style status JSON with one AMS unit holding the 4 IFS slots."""
    trays = []
    exist = 0
    for i in range(4):
        lane = lanes.get("lane%d" % (i + 1)) or {}
        mat = lane.get("material") or ""
        col = (lane.get("color") or "").lstrip("#")
        tray = {"id": str(i)}
        if mat:
            exist |= 1 << i
            tray["tray_type"] = mat
            tray["tray_sub_brands"] = mat
            tray["tray_color"] = (col.upper() + "FF") if len(col) == 6 else "00000000"
            tray["tray_info_idx"] = ""
            tray["tray_diameter"] = "1.75"
            tray["tray_weight"] = "1000"
            try:
                nt = int(lane.get("nozzle_temp") or 0)
            except Exception:
                nt = 0
            if nt > 0:
                tray["nozzle_temp_min"] = str(max(nt - 20, 0))
                tray["nozzle_temp_max"] = str(nt + 20)
        trays.append(tray)
    bits = format(exist, "x")
    loaded, auto_refill = ifs_state(live)
    now = str(loaded) if loaded is not None else "255"
    ams = {
        "ams": [{"id": "0", "humidity": "5", "temp": "0", "tray": trays}],
        "ams_exist_bits": "1",
        "tray_exist_bits": bits,
        "tray_is_bbl_bits": "0",
        "tray_read_done_bits": bits,
        "tray_reading_bits": "0",
        "tray_tar": "255",
        "tray_now": now,
        "tray_pre": "255",
        "version": 1,
        "insert_flag": True,
        "power_on_flag": False,
    }
    status = {
        "print": {
            "command": "push_status",
            "msg": 0,
            "sequence_id": "0",
            "gcode_state": "IDLE",
            # The Send dialog refuses to print when the printer reports no
            # storage. Kiln stores files on the printer itself, so say it has
            # a healthy "SD card" (both the old boolean and the home_flag bit).
            "sdcard": True,
            # bit 8 = SD card present, bit 10 = AMS auto refill ("backup")
            "home_flag": 256 | (1 << 10 if auto_refill else 0),
            "ams": ams,
            "ams_status": 0,
            "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000"},
        }
    }
    status["print"].update(live_fields(live))
    # No "printer_type": the Send dialog compares it with the type of the
    # printer preset, and the Flashforge presets have an empty type. Any value
    # here (e.g. "AD5X") shows the printer as "Unknown" and blocks Send.
    return status


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------
def notify(text, level="info"):
    """Show a short message in Orca's 3D view (best effort, never raises)."""
    log.info("notify: %s", text)
    try:
        orca.host.ui.push_notification(level, "Kiln IFS: " + text)
    except Exception:
        try:
            orca.host.ui.push_notification("warning", "Kiln IFS: " + text)
        except Exception:
            pass


class KilnIfsAgent(pa.PrinterAgentBase):
    def __init__(self):
        super().__init__()
        self.callbacks = {}
        self.selected = ""
        self.client = None
        self.dev_id = ""
        self.ids = []  # every device id Orca may use for this printer
        self._stop = threading.Event()
        self._thread = None
        self._cfg = dict(DEFAULT_CONFIG)

    # -- configuration (Config tab) ---------------------------------------
    def get_default_config(self):
        return dict(DEFAULT_CONFIG)

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return CONFIG_UI

    def on_load(self):
        cfg = self.refresh_cfg()
        notify("agent " + VERSION + " loaded, printer address: %s" % (cfg["_host"] or "(from preset)"))

    def refresh_cfg(self):
        """Read the saved config. Never raises: falls back to the defaults."""
        data = dict(DEFAULT_CONFIG)
        try:
            saved = json.loads(self.get_config() or "{}")
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            log.exception("could not read plugin config")
        try:
            host, port = normalize_host(str(data.get("printer_ip") or ""))
            data["_host"] = host
            data["_port"] = port or str(data.get("port") or "").strip()
        except Exception:
            log.exception("bad plugin config values")
            data["_host"], data["_port"] = "", ""
        self._cfg = data
        return data

    # -- identity ----------------------------------------------------------
    def get_name(self):
        return "Kiln OS IFS Agent"

    def get_agent_info(self):
        return pa.AgentInfo(AGENT_ID, "Kiln OS (IFS)", VERSION, "Flashforge AD5X on Kiln OS: IFS slots and slot mapping")

    # -- host callbacks ----------------------------------------------------
    def _store(self, key, fn):
        self.callbacks[key] = fn
        return 0

    def set_on_ssdp_msg_fn(self, fn): return self._store("ssdp", fn)
    def set_on_local_connect_fn(self, fn): return self._store("local_connect", fn)
    def set_on_local_message_fn(self, fn): return self._store("local_message", fn)
    def set_on_message_fn(self, fn): return self._store("message", fn)
    def set_on_user_message_fn(self, fn): return self._store("user_message", fn)
    def set_on_printer_connected_fn(self, fn): return self._store("printer_connected", fn)
    def set_on_subscribe_failure_fn(self, fn): return self._store("subscribe_failure", fn)
    def set_server_callback(self, fn): return self._store("server_error", fn)
    def set_queue_on_main_fn(self, fn): return self._store("queue_on_main", fn)

    def _emit(self, key, *args):
        fn = self.callbacks.get(key)
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                log.exception("callback %s failed", key)

    # -- discovery / selection ---------------------------------------------
    def _announce(self, ip):
        """Tell the Device tab that a printer exists at `ip`."""
        if not ip:
            return
        self._emit("ssdp", json.dumps({
            "dev_id": ip,
            "dev_name": PRINTER_NAME,
            "dev_ip": ip,
            "dev_type": "",
            "dev_signal": "",
            "connect_type": "lan",
            "bind_state": "free",
        }))

    def start_discovery(self, start, sending):
        if start:
            self._announce(self.selected)
            self._announce(self.dev_id)
            self._announce(self._cfg.get("_host", ""))
        return True

    def get_user_selected_machine(self):
        return self.selected

    def set_user_selected_machine(self, dev_id):
        self.selected = dev_id
        if dev_id:
            self._announce(dev_id)
        return 0

    def bind_detect(self, dev_ip, sec_link, detect):
        detect.dev_id = dev_ip
        detect.dev_name = PRINTER_NAME
        detect.model_id = ""
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return 0

    def check_cert(self): return 0
    def ping_bind(self, ping_code): return 0
    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn): return 0
    def unbind(self, dev_id): return 0
    def request_bind_ticket(self): return (0, "")

    # -- connection --------------------------------------------------------
    def connect_printer(self, params):
        try:
            cfg = self._cfg
            host = cfg.get("_host") or params.host
            port = cfg.get("_port") or params.port
            self.client = Moonraker(host, port)
            self.dev_id = params.dev_id or host
            ids = [params.dev_id, host, ("%s:%s" % (host, port)) if port else "",
                   params.host, ("%s:%s" % (params.host, params.port)) if params.port else "",
                   self.selected, cfg.get("_host", "")]
            self.ids = []
            for i in ids:
                if i and i not in self.ids:
                    self.ids.append(i)
            log.info("connecting to %s (port %r), ids %s", host, port, self.ids)
            self.client.get("/server/info")
        except Exception as e:
            log.error("connect failed: %s", e)
            notify("cannot reach %s: %s" % (params.host, e), "error")
            self._emit("local_connect", 1, params.dev_id, str(e))
            return -1
        notify("connected to %s, ids: %s" % (host, ", ".join(self.ids)))
        self._first_push = True
        for i in self.ids or [self.dev_id]:
            self._announce(i)
            self._emit("local_connect", 0, i, "")
        self._start_poll()
        return 0

    def disconnect_printer(self):
        self._stop.set()
        self.client = None
        return 0

    def _start_poll(self):
        self._stop.set()
        self._stop = threading.Event()
        stop = self._stop

        def loop():
            # Orca may not have the device ready for the very first push, so
            # repeat it every second for a while, then settle to the poll rate.
            burst = 6
            while not stop.is_set():
                self._push_lanes()
                if burst > 0:
                    burst -= 1
                    stop.wait(2)
                else:
                    stop.wait(POLL_SECONDS)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def _push_lanes(self):
        client = self.client
        if client is None:
            return False
        try:
            live = client.live()
        except Exception as e:
            log.warning("live status read failed: %s", e)
            live = None
        lanes = lanes_from_live(live)
        if lanes is None:
            try:
                lanes = client.lane_data()
            except Exception as e:
                log.warning("lane_data read failed: %s", e)
                return False
        payload = json.dumps(build_status(lanes, live))
        if getattr(self, "_first_push", False):
            self._first_push = False
            notify("sent slots to Orca: " + ", ".join(
                "A%d %s" % (n + 1, (lanes.get("lane%d" % (n + 1)) or {}).get("material") or "-")
                for n in range(4)))
        for i in self.ids or [self.dev_id]:
            self._emit("local_message", i, json.dumps(build_version_info(i)))
            self._emit("local_message", i, payload)
        return True

    # -- filament sync -----------------------------------------------------
    def get_filament_sync_mode(self):
        return pa.FilamentSyncMode.Pull

    def fetch_filament_info(self, dev_id, sync_mode):
        return self._push_lanes()

    # -- commands ----------------------------------------------------------
    def send_message(self, dev_id, json_str, qos, flag):
        return -7010

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        """Translate the Device tab's Bambu-style commands into Moonraker calls."""
        client = self.client
        msg = parse_json(json_str)
        if client is None or not isinstance(msg, dict):
            return -1
        # Requests for information: answer by pushing the status again.
        if "info" in msg or "pushing" in msg:
            threading.Thread(target=self._push_lanes, daemon=True).start()
            return 0
        p = msg.get("print") or msg.get("system") or {}
        cmd = p.get("command")
        try:
            if cmd == "gcode_line":
                client.gcode(str(p.get("param", "")).strip())
            elif cmd == "pause":
                client.control("pause")
            elif cmd == "resume":
                client.control("resume")
            elif cmd == "stop":
                client.control("cancel")
            elif cmd == "print_speed":
                pct = {"1": 50, "2": 100, "3": 124, "4": 166}.get(str(p.get("param")), 100)
                client.gcode("M220 S%d" % pct)
            elif cmd == "ledctrl":
                white = 1 if str(p.get("led_mode", "")).lower() == "on" else 0
                client.gcode('SET_LED LED="chamber_led" RED=0 GREEN=0 BLUE=0 WHITE=%d SYNC=0 TRANSMIT=1' % white)
            elif cmd == "ams_filament_setting":
                # Slot edit dialog (click a slot): material, colour, temperature.
                line = filament_setting_gcode(p)
                if not line:
                    return -1
                client.gcode(line)
                notify(line)
            elif cmd == "ams_change_filament":
                # Load / Unload buttons. These take a while, so run them in
                # the background and answer the Device tab right away.
                line = change_filament_gcode(p)
                if not line:
                    return -1
                self._run_slow(line)
            elif cmd == "print_option" and "auto_switch_filament" in p:
                on = 1 if p.get("auto_switch_filament") else 0
                client.gcode("IFS_AUTO_REFILL ENABLE=%d" % on)
            elif cmd in ("ams_user_setting", "extrusion_cali_sel", "extrusion_cali_get"):
                pass  # nothing to do on the IFS; just acknowledge
            else:
                log.info("unsupported command: %s", json_str[:200])
                return -7010
        except Exception as e:
            log.error("command %s failed: %s", cmd, e)
            notify("command %s failed: %s" % (cmd, e), "error")
            return -1
        self._ack(p)
        threading.Thread(target=self._push_lanes, daemon=True).start()
        return 0

    def _ack(self, p):
        """Answer a command the way a Bambu printer does, so dialogs close."""
        reply = {"print": dict(p, result="success", reason="")}
        for i in self.ids or [self.dev_id]:
            self._emit("local_message", i, json.dumps(reply))

    def _run_slow(self, line):
        client = self.client

        def run():
            notify(line)
            try:
                client.gcode(line, timeout=600)
            except Exception as e:
                log.error("%s failed: %s", line, e)
                notify("%s failed: %s" % (line, e), "error")
            self._push_lanes()

        threading.Thread(target=run, daemon=True).start()

    # -- jobs --------------------------------------------------------------
    def _report(self, fn, status, code, msg):
        if fn is not None:
            try:
                fn(status, code, msg)
            except Exception:
                log.exception("progress callback failed")

    def _cancelled(self, cancel_fn):
        try:
            return bool(cancel_fn()) if cancel_fn is not None else False
        except Exception:
            return False

    def _upload_job(self, params, update_fn, cancel_fn):
        client = self.client
        if client is None:
            cfg = self._cfg
            client = Moonraker(cfg.get("_host") or params.dev_ip or self.dev_id, cfg.get("_port", ""))
            self.client = client
        name = safe_name(params.project_name or params.task_name or os.path.basename(params.filename))
        self._report(update_fn, ST_UPLOAD, 0, "Preparing G-code")
        data = read_gcode(params.filename, int(getattr(params, "plate_index", 1) or 1))
        if self._cancelled(cancel_fn):
            return None
        self._report(update_fn, ST_UPLOAD, 0, "Uploading " + name)
        client.upload(name, data)
        return name

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        """Send = upload only. 'verify_job' is just a reachability check."""
        try:
            if params.project_name == "verify_job":
                cfg = self._cfg
                Moonraker(cfg.get("_host") or params.dev_ip or self.dev_id, cfg.get("_port", "")).get("/server/info")
                return 0
            name = self._upload_job(params, update_fn, cancel_fn)
            if name is None:
                return -1
            self._report(update_fn, ST_DONE, 0, "Uploaded " + name)
            return 0
        except Exception as e:
            log.error("upload failed: %s", e)
            self._report(update_fn, ST_ERROR, 1, str(e))
            return -1

    def start_local_print(self, params, update_fn, cancel_fn):
        try:
            name = self._upload_job(params, update_fn, cancel_fn)
            if name is None:
                return -1
            self._report(update_fn, ST_SEND, 0, "Applying filament slots")
            mapping = mapping_from_params(params)
            lines = ["IFS_MAP_TOOL TOOL=%d SLOT=%d" % (t, s) for t, s in sorted(mapping.items())]
            for line in lines:
                self.client.gcode(line)
            log.info("slot mapping applied: %s", lines)
            if self._cancelled(cancel_fn):
                return -1
            self._report(update_fn, ST_SEND, 0, "Starting print")
            self.client.start_print(name)
            self._report(update_fn, ST_DONE, 0, "Print started")
            return 0
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
            log.error("print start failed: %s %s", e, detail)
            self._report(update_fn, ST_ERROR, e.code, "%s %s" % (e, detail))
            return -1
        except Exception as e:
            log.error("print start failed: %s", e)
            self._report(update_fn, ST_ERROR, 1, str(e))
            return -1

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return -7010

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        return -7010


@orca.plugin
class KilnIfsPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(KilnIfsAgent)
