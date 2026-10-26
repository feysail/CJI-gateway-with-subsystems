"""
CJI Gateway Unified Controller — server.py
Merges:
  • RGB/RGBW control (TCP socket → port 5021/5022)
  • GPIO port control (HTTP POST → apply.cgi with admin:admin)
Serves static/ folder for the frontend.
Run: python server.py
"""

import sys
import os
import time
import socket
import json
import math
import random
import threading
import base64
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─────────────────────────────────────────────
#  Gateway defaults
# ─────────────────────────────────────────────
GATEWAY_IP   = "192.168.11.1"
GATEWAY_USER = "admin"
GATEWAY_PASS = "admin"
DEFAULT_PORT  = 5021
DEFAULT_ADDR  = 0xFF

# GPIO pin map:  port-name → (gpio_pin, inverted)
# Note: On WMD-7621U hardware, GPIO 13 is the physical AC relay (Port 1).
GPIO_MAP = {
    "1":     (13, False),
    "2":     (14, False),
    "3":     (15, False),
    "relay": (13, False),  # Port 1 is the physical Relay
}

# ─────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────
state_lock = threading.Lock()

CURRENT_STATE = {
    "gateway_ip":    GATEWAY_IP,
    "gateway_port":  DEFAULT_PORT,
    "slave_addr":    DEFAULT_ADDR,
    "protocol_mode": "both",
    "r": 255, "g": 0, "b": 0, "w": 0,
    "brightness": 100,
    "power": True,
    "active_effect": None,
    "last_tx_hex":  "",
    "last_error":   None,
    "last_tx_time": None,
    "packet_history": [],
    # port states:  True = ON
    "ports": {
        "1":     False,
        "2":     False,
        "3":     False,
        "relay": False,
    },
    "port_errors": {},
    # Environmental sensor telemetry state
    "sensor": {
        "last_data": None,
        "last_poll_time": None,
        "last_error": None,
        "sensor_port": DEFAULT_PORT,
        "sensor_addr": DEFAULT_ADDR,
        "history": [],
    },
}

effect_stop_event = threading.Event()
effect_thread     = None


# ─────────────────────────────────────────────
#  Gateway Command Execution (Telnet primary, HTTP fallback)
# ─────────────────────────────────────────────
def _telnet_cmd(cmd, ip=GATEWAY_IP, user=GATEWAY_USER, pw=GATEWAY_PASS, timeout=3.0):
    """Executes command over Telnet (Port 23) — zero HTTP session lockouts."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((ip, 23))

    def read_until(token):
        buf = b""
        start = time.time()
        while time.time() - start < timeout:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
            if token in buf:
                return buf
        return buf

    read_until(b"login: ")
    s.sendall(user.encode("ascii") + b"\n")
    read_until(b"Password: ")
    s.sendall(pw.encode("ascii") + b"\n")
    read_until(b"# ")
    s.sendall(cmd.encode("utf-8") + b"\n")
    out = read_until(b"# ")
    s.close()

    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.split("\n") if l.strip() and not l.strip().endswith("#") and cmd not in l]
    return "\n".join(lines).strip()


def _gateway_cmd(cmd, ip=GATEWAY_IP, user=GATEWAY_USER, pw=GATEWAY_PASS):
    # Try Telnet first: fast (50ms) and never suffers from single-user web lockouts
    try:
        res = _telnet_cmd(cmd, ip, user, pw)
        if res:
            return res
    except Exception:
        pass

    # HTTP apply.cgi fallback
    creds   = base64.b64encode(f"{user}:{pw}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    body = urllib.parse.urlencode({
        "action_mode":  " SystemCmd ",
        "current_page": "console_response.asp",
        "next_page":    "console_response.asp",
        "SystemCmd":    cmd,
    }).encode()

    req = urllib.request.Request(
        f"http://{ip}/apply.cgi",
        data=body, headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass

    time.sleep(0.45)

    req2 = urllib.request.Request(
        f"http://{ip}/console_response.asp",
        headers={"Authorization": f"Basic {creds}"},
    )
    try:
        with urllib.request.urlopen(req2, timeout=4) as resp:
            return resp.read().decode(errors="replace").strip()
    except Exception as e:
        return f"ERROR: {e}"


# ─────────────────────────────────────────────
#  Port (GPIO) control with Flash State Persistence
# ─────────────────────────────────────────────
def set_port(port_name, on, ip=GATEWAY_IP, user=GATEWAY_USER, pw=GATEWAY_PASS):
    key = str(port_name).lower()
    if key not in GPIO_MAP:
        return {"success": False, "error": f"Unknown port '{port_name}'"}
    pin, inverted = GPIO_MAP[key]
    hw_val = (0 if on else 1) if inverted else (1 if on else 0)

    # Persist state into gateway /etc/storage and /userSpace so state survives complete power loss:
    state_file = "port1_state" if key in ("1", "relay") else f"port{key}_state"
    save_txt_snippet = ""
    if key in ("1", "relay"):
        save_txt_snippet = f" && sed -i -r 's/^[0-9]+-(.*)/{hw_val}-\\1/' /userSpace/bin/save.txt"
    persist_snippet = (
        f" && echo {hw_val} > /etc/storage/{state_file}"
        f"{save_txt_snippet}"
        f" && sync"
        f" && (/sbin/mtd_storage.sh save >/dev/null 2>&1 &)"
    )
    cmd = f"mtk_gpio -d {pin} 1 && mtk_gpio -w {pin} {hw_val} && mtk_gpio -r {pin}{persist_snippet}"

    try:
        resp = _gateway_cmd(cmd, ip, user, pw)
        with state_lock:
            CURRENT_STATE["ports"][key] = on
            CURRENT_STATE["port_errors"].pop(key, None)
            # Keep Port 1 and Relay in sync since both refer to physical GPIO 13 relay
            if key in ("1", "relay"):
                CURRENT_STATE["ports"]["1"] = on
                CURRENT_STATE["ports"]["relay"] = on
        val_str = "1" if on else "0"
        return {"success": True, "port": key, "state": on, "raw": val_str, "persisted": True}
    except Exception as e:
        err = str(e)
        with state_lock:
            CURRENT_STATE["port_errors"][key] = err
        return {"success": False, "error": err}


def get_port(port_name, ip=GATEWAY_IP, user=GATEWAY_USER, pw=GATEWAY_PASS):
    key = str(port_name).lower()
    if key not in GPIO_MAP:
        return {"success": False, "error": f"Unknown port '{port_name}'"}
    pin, inverted = GPIO_MAP[key]
    cmd = f"mtk_gpio -r {pin}"
    try:
        resp = _gateway_cmd(cmd, ip, user, pw).strip()
        raw_val = None
        for part in resp.split():
            if part.isdigit():
                raw_val = int(part)
        if raw_val is None:
            raise ValueError(f"Unexpected response: {resp}")
        on = (raw_val == 0) if inverted else (raw_val == 1)
        with state_lock:
            CURRENT_STATE["ports"][key] = on
            if key in ("1", "relay"):
                CURRENT_STATE["ports"]["1"] = on
                CURRENT_STATE["ports"]["relay"] = on
        return {"success": True, "port": key, "state": on, "raw": "1" if on else "0"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def refresh_all_ports(ip=GATEWAY_IP, user=GATEWAY_USER, pw=GATEWAY_PASS):
    for name in GPIO_MAP:
        get_port(name, ip, user, pw)
        time.sleep(0.1)


# ─────────────────────────────────────────────
#  RGB / RGBW packet helpers
# ─────────────────────────────────────────────
def build_a0_packet(r, g, b, w=0, addr=0xFF):
    return bytes([0xA5, 0x07, addr & 0xFF, 0xA0,
                  r & 0xFF, g & 0xFF, b & 0xFF, w & 0xFF, 0xEE, 0x5A])

def build_a1_channel_packet(channel, value, addr=0xFF):
    channel = max(1, min(4, int(channel)))
    return bytes([0xA5, 0x07, addr & 0xFF, 0xA1, 0x03,
                  channel, value & 0xFF, 0x01, 0xEE, 0x5A])

def build_jump7_packet(addr=0xFF):
    """Hardware Macro: 7-Color Jump Flash (SSCOM N15)"""
    return bytes([0xA5, 0x0A, addr & 0xFF, 0xA1, 0x02,
                  0x06, 0x10, 0x10, 0xFF, 0x08, 0xFF, 0xEE, 0x5A])

def build_breathe_packet(addr=0xFF):
    """Hardware Macro: Breathing Light Fade (0% - 100% Sinusoidal PWM ramping)"""
    return bytes([0xA5, 0x07, addr & 0xFF, 0xA1, 0x01,
                  0x06, 0x00, 0xFF, 0xEE, 0x5A])

def build_rainbow_packet(addr=0xFF):
    """Hardware Macro: Rainbow Smooth Fade (HSV chromatic circle)"""
    return bytes([0xA5, 0x0A, addr & 0xFF, 0xA1, 0x02,
                  0x01, 0x10, 0x10, 0xFF, 0x08, 0xFF, 0xEE, 0x5A])

def build_strobe_packet(addr=0xFF):
    """Hardware Macro: High-Intensity White Strobe / Alert Beacon"""
    return bytes([0xA5, 0x0A, addr & 0xFF, 0xA1, 0x02,
                  0x08, 0x05, 0x05, 0xFF, 0x08, 0xFF, 0xEE, 0x5A])

def build_query_rgb_packet(addr=0xFF):
    """SSCOM N11: Query RGB output values"""
    return bytes([0xA5, 0x03, addr & 0xFF, 0xA0, 0xEE, 0x5A])

def build_query_addr_packet():
    """SSCOM N10: Query device address"""
    return bytes([0xA5, 0x04, 0xFF, 0xB1, 0x00, 0xEE, 0x5A])

def build_query_baud_packet(addr=0xFF):
    """SSCOM N28: Query baud rate"""
    return bytes([0xA5, 0x03, addr & 0xFF, 0xB5, 0xEE, 0x5A])

def build_set_baud_packet(baud_code=1, addr=0xFF):
    """SSCOM N12 (0=4800bps), N27 (1=9600bps)"""
    return bytes([0xA5, 0x04, addr & 0xFF, 0xB4, baud_code & 0xFF, 0xEE, 0x5A])

def send_to_gateway(packet, ip, port, timeout=1.0):
    tx_hex  = packet.hex(" ").upper()
    err_str = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(packet)
        time.sleep(0.04)
        s.close()
    except Exception as e:
        err_str = str(e)

    with state_lock:
        CURRENT_STATE["last_tx_hex"]  = tx_hex
        CURRENT_STATE["last_error"]   = err_str
        CURRENT_STATE["last_tx_time"] = time.strftime("%H:%M:%S")
        entry = {
            "time":   time.strftime("%H:%M:%S"),
            "tx":     tx_hex,
            "status": ("ERROR: " + err_str) if err_str else "SENT OK",
        }
        CURRENT_STATE["packet_history"].insert(0, entry)
        if len(CURRENT_STATE["packet_history"]) > 50:
            CURRENT_STATE["packet_history"].pop()

    return {"success": err_str is None, "tx": tx_hex, "error": err_str}

def dispatch_color(r, g, b, w, ip, port, addr, mode="both"):
    results = []
    if mode in ["a0", "both"]:
        pkt = build_a0_packet(r, g, b, w, addr)
        results.append(send_to_gateway(pkt, ip, port))
    if mode in ["a1", "both"]:
        time.sleep(0.03)
        for ch, val in [(1, r), (2, g), (3, b), (4, w)]:
            send_to_gateway(build_a1_channel_packet(ch, val, addr), ip, port)
            time.sleep(0.02)
    with state_lock:
        CURRENT_STATE["r"] = r
        CURRENT_STATE["g"] = g
        CURRENT_STATE["b"] = b
        CURRENT_STATE["w"] = w
    return results[0] if results else {"success": True}

def effect_worker(effect_name, ip, port, addr, speed=1.0):
    speed = max(0.2, min(5.0, float(speed)))
    try:
        if effect_name == "jump7":
            # SSCOM N15: 7-Color Jump Flash hardware macro
            send_to_gateway(build_jump7_packet(addr), ip, port)
        elif effect_name == "breathe":
            # Hardware Macro: Autonomous Breathing Light Fade
            send_to_gateway(build_breathe_packet(addr), ip, port)
        elif effect_name == "breathe_color":
            # Sinusoidal breathing fade on the currently active color selection
            with state_lock:
                base_r = CURRENT_STATE.get("r", 255)
                base_g = CURRENT_STATE.get("g", 0)
                base_b = CURRENT_STATE.get("b", 0)
                base_w = CURRENT_STATE.get("w", 0)
            if base_r == 0 and base_g == 0 and base_b == 0 and base_w == 0:
                base_r, base_g, base_b = 255, 0, 128
            step = 0.0
            while not effect_stop_event.is_set():
                factor = 0.04 + 0.96 * (0.5 * (1.0 + math.sin(step)))
                cr = int(base_r * factor)
                cg = int(base_g * factor)
                cb = int(base_b * factor)
                cw = int(base_w * factor)
                pkt = build_a0_packet(cr, cg, cb, cw, addr)
                send_to_gateway(pkt, ip, port)
                step += 0.16 * speed
                effect_stop_event.wait(max(0.03, 0.06 / speed))
        elif effect_name == "rainbow":
            # Trigger hardware macro first, then cycle smooth software spectrum
            send_to_gateway(build_rainbow_packet(addr), ip, port)
            colors = [
                (255, 0, 0, 0), (255, 128, 0, 0), (255, 255, 0, 0),
                (0, 255, 0, 0), (0, 255, 255, 0), (0, 0, 255, 0),
                (147, 51, 234, 0), (236, 72, 153, 0)
            ]
            idx = 0
            while not effect_stop_event.is_set():
                dispatch_color(*colors[idx], ip, port, addr, "both")
                idx = (idx + 1) % len(colors)
                effect_stop_event.wait(max(0.2, 0.9 / speed))
        elif effect_name == "strobe":
            # Trigger hardware strobe macro and maintain high-rate pulse train
            send_to_gateway(build_strobe_packet(addr), ip, port)
            while not effect_stop_event.is_set():
                dispatch_color(255, 255, 255, 255, ip, port, addr, "both")
                effect_stop_event.wait(max(0.05, 0.18 / speed))
                if effect_stop_event.is_set():
                    break
                dispatch_color(0, 0, 0, 0, ip, port, addr, "both")
                effect_stop_event.wait(max(0.05, 0.18 / speed))
        elif effect_name == "candle":
            # Warm organic candlelight flicker
            while not effect_stop_event.is_set():
                flicker = random.randint(140, 255)
                flicker_g = int(flicker * random.uniform(0.32, 0.45))
                dispatch_color(flicker, flicker_g, 0, 0, ip, port, addr, "a0")
                effect_stop_event.wait(random.uniform(0.05, 0.18) / speed)
        elif effect_name == "jump3":
            # Primary Tri-Color Jump (Red -> Green -> Blue)
            colors = [(255, 0, 0, 0), (0, 255, 0, 0), (0, 0, 255, 0)]
            idx = 0
            while not effect_stop_event.is_set():
                dispatch_color(*colors[idx], ip, port, addr, "both")
                idx = (idx + 1) % len(colors)
                effect_stop_event.wait(max(0.2, 0.6 / speed))
    except Exception as e:
        print("Effect error:", e)


# ─────────────────────────────────────────────
#  Environmental Sensor (Modbus RTU)
# ─────────────────────────────────────────────
def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def make_sensor_read_command(slave_addr: int = 0xFF, start_reg: int = 0x0001, count: int = 22) -> bytes:
    pdu = bytes([slave_addr & 0xFF, 0x03, (start_reg >> 8) & 0xFF, start_reg & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    return pdu + crc16_modbus(pdu)

def query_sensor(ip=GATEWAY_IP, port=DEFAULT_PORT, slave_addr=DEFAULT_ADDR, timeout=2.0):
    """
    Queries 22 environmental sensor registers (0x0001 - 0x0016) via transparent TCP bridge.
    """
    req = make_sensor_read_command(slave_addr, 0x0001, 22)
    tx_hex = req.hex(" ").upper()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))

        # Clear any buffered pending bytes
        s.setblocking(False)
        try:
            while True:
                if not s.recv(1024):
                    break
        except Exception:
            pass
        s.setblocking(True)
        s.settimeout(timeout)

        s.sendall(req)
        time.sleep(0.2)
        res = b""
        start = time.time()
        while time.time() - start < timeout:
            try:
                chunk = s.recv(1024)
                if not chunk:
                    break
                res += chunk
                if len(res) >= 5 and len(res) >= (3 + res[2] + 2):
                    break
            except socket.timeout:
                break
        s.close()

        if len(res) < 5:
            raise ConnectionError(f"No response or timeout from {ip}:{port} (got {len(res)} bytes). Verify physical RS-485 wiring & Port 1 Relay.")

        if res[1] != 0x03:
            raise ValueError(f"Modbus exception / invalid function code: 0x{res[1]:02X}")

        # Check CRC
        expected_crc = crc16_modbus(res[:-2])
        actual_crc = res[-2:]
        if expected_crc != actual_crc:
            raise ValueError(f"CRC Mismatch! Expected 0x{expected_crc.hex().upper()}, got 0x{actual_crc.hex().upper()}")

        byte_count = res[2]
        data_bytes = res[3:3+byte_count]
        regs = [int.from_bytes(data_bytes[i:i+2], byteorder='big') for i in range(0, len(data_bytes), 2)]

        parsed = {
            "success": True,
            "simulated": False,
            "timestamp": time.strftime("%H:%M:%S"),
            "full_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "slave_address": f"0x{res[0]:02X}",
            "port": port,
            "noise": round(regs[0] / 10.0, 1) if len(regs) > 0 else 0,
            "pm25": regs[6] if len(regs) > 6 else 0,
            "pm10": regs[7] if len(regs) > 7 else 0,
            "temperature": round((regs[8] / 100.0) - 40.0, 2) if len(regs) > 8 else 0,
            "humidity": round(regs[9] / 100.0, 2) if len(regs) > 9 else 0,
            "pressure": round(regs[10] / 10.0, 1) if len(regs) > 10 else 0,
            "wind_speed": round(regs[11] / 100.0, 2) if len(regs) > 11 else 0,
            "wind_direction": round(regs[12] / 10.0, 1) if len(regs) > 12 else 0,
            "rainfall": round(regs[13] / 10.0, 1) if len(regs) > 13 else 0,
            "solar_radiation": regs[14] if len(regs) > 14 else 0,
            "illuminance": (regs[15] * 10) if len(regs) > 15 else 0,
            "uv_index": regs[16] if len(regs) > 16 else 0,
            "co2": regs[17] if len(regs) > 17 else 0,
            "negative_ions": (regs[18] * 10) if len(regs) > 18 else 0,
            "tx_hex": tx_hex,
            "raw_hex": res.hex(" ").upper()
        }

        with state_lock:
            CURRENT_STATE["sensor"]["last_data"] = parsed
            CURRENT_STATE["sensor"]["last_poll_time"] = parsed["timestamp"]
            CURRENT_STATE["sensor"]["last_error"] = None
            CURRENT_STATE["sensor"]["history"].insert(0, parsed)
            if len(CURRENT_STATE["sensor"]["history"]) > 30:
                CURRENT_STATE["sensor"]["history"].pop()

        return parsed

    except Exception as e:
        err_msg = str(e)
        with state_lock:
            CURRENT_STATE["sensor"]["last_error"] = err_msg
        return {
            "success": False,
            "error": err_msg,
            "timestamp": time.strftime("%H:%M:%S"),
            "tx_hex": tx_hex,
            "port": port,
            "last_cached": CURRENT_STATE["sensor"].get("last_data")
        }


# ─────────────────────────────────────────────
#  HTTP handler
# ─────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css",
    ".js":   "application/javascript",
    ".json": "application/json",
    ".png":  "image/png",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
}

class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        ext  = os.path.splitext(path)[1].lower()
        mime = MIME_MAP.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/api/status":
            with state_lock:
                self._send_json(dict(CURRENT_STATE))
            return

        if path == "/api/sensor" or path == "/api/sensor/data":
            query_map = parse_qs(parsed.query)
            ip_val    = query_map.get("ip", [GATEWAY_IP])[0]
            try:
                port_val = int(query_map.get("port", [str(DEFAULT_PORT)])[0])
            except Exception:
                port_val = DEFAULT_PORT
            addr_str  = query_map.get("addr", [str(DEFAULT_ADDR)])[0]

            try:
                addr_val = int(addr_str, 16) if ("0x" in addr_str.lower() or "ff" in addr_str.lower()) else int(addr_str)
            except Exception:
                addr_val = DEFAULT_ADDR

            data = query_sensor(ip=ip_val, port=port_val, slave_addr=addr_val)
            self._send_json(data)
            return

        if path == "/api/sensor/history":
            with state_lock:
                self._send_json({"history": CURRENT_STATE["sensor"].get("history", [])})
            return

        if path.startswith("/api/port/get/"):
            port_name = path.split("/")[-1]
            result = get_port(port_name)
            self._send_json(result)
            return

        if path == "/" or path == "/index.html":
            path = "/index.html"

        file_path = os.path.join(STATIC_DIR, path.lstrip("/").replace("/", os.sep))
        if os.path.isfile(file_path):
            self._send_file(file_path)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global effect_thread, effect_stop_event
        parsed = urlparse(self.path)
        path   = parsed.path
        clen   = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(clen).decode("utf-8") if clen > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = {}

        gw_ip = params.get("ip",   GATEWAY_IP)
        port  = int(params.get("port", DEFAULT_PORT))
        addr  = int(params.get("addr", DEFAULT_ADDR))
        mode  = params.get("mode", "both")

        # Stop effects on any RGB command
        if path in ["/api/rgb", "/api/raw", "/api/channel"]:
            effect_stop_event.set()

        if path == "/api/rgb":
            r = int(params.get("r", 255))
            g = int(params.get("g", 0))
            b = int(params.get("b", 0))
            w = int(params.get("w", 0))
            res = dispatch_color(r, g, b, w, gw_ip, port, addr, mode)
            self._send_json(res)

        elif path == "/api/channel":
            channel = int(params.get("channel", 1))
            val     = int(params.get("value", 255))
            pkt     = build_a1_channel_packet(channel, val, addr)
            self._send_json(send_to_gateway(pkt, gw_ip, port))

        elif path == "/api/raw":
            hex_str = params.get("hex", "").replace(" ", "").replace("0x", "")
            try:
                pkt = bytes.fromhex(hex_str)
                self._send_json(send_to_gateway(pkt, gw_ip, port))
            except Exception as e:
                self._send_json({"success": False, "error": f"Invalid Hex: {e}"}, code=400)

        elif path == "/api/effect":
            effect_name = params.get("effect", "jump7")
            speed = float(params.get("speed", 1.0))
            effect_stop_event.set()
            if effect_thread and effect_thread.is_alive():
                effect_thread.join(timeout=0.5)

            if effect_name != "stop":
                effect_stop_event.clear()
                effect_thread = threading.Thread(
                    target=effect_worker,
                    args=(effect_name, gw_ip, port, addr, speed),
                    daemon=True,
                )
                effect_thread.start()
                self._send_json({"success": True, "effect": effect_name, "speed": speed})
            else:
                self._send_json({"success": True, "effect": "stop", "stopped": True})

        elif path == "/api/query":
            # SSCOM Diagnostics & State Queries
            q_type   = params.get("type", "rgb")
            q_addr   = int(params.get("addr", DEFAULT_ADDR))
            q_ip     = params.get("ip", GATEWAY_IP)
            q_port   = int(params.get("port", DEFAULT_PORT))

            if q_type == "rgb":
                pkt = build_query_rgb_packet(q_addr)
            elif q_type == "addr":
                pkt = build_query_addr_packet()
            elif q_type == "baud":
                pkt = build_query_baud_packet(q_addr)
            else:
                pkt = build_query_rgb_packet(q_addr)

            tx_hex  = pkt.hex(" ").upper()
            rx_hex  = ""
            err_str = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.2)
                s.connect((q_ip, q_port))
                s.sendall(pkt)
                time.sleep(0.08)
                try:
                    data = s.recv(256)
                    if data:
                        rx_hex = data.hex(" ").upper()
                except Exception:
                    pass
                s.close()
            except Exception as e:
                err_str = str(e)

            with state_lock:
                CURRENT_STATE["last_tx_hex"]  = tx_hex
                CURRENT_STATE["last_error"]   = err_str
                CURRENT_STATE["last_tx_time"] = time.strftime("%H:%M:%S")
                entry = {
                    "time":   time.strftime("%H:%M:%S"),
                    "tx":     tx_hex,
                    "rx":     rx_hex,
                    "status": f"RECV: {rx_hex}" if rx_hex else ("ERROR: " + err_str if err_str else "QUERY SENT"),
                }
                CURRENT_STATE["packet_history"].insert(0, entry)
                if len(CURRENT_STATE["packet_history"]) > 50:
                    CURRENT_STATE["packet_history"].pop()

            self._send_json({"success": err_str is None, "type": q_type, "tx": tx_hex, "rx": rx_hex, "error": err_str})

        elif path == "/api/port/set":
            port_name = str(params.get("port", ""))
            on        = bool(params.get("state", False))
            result    = set_port(port_name, on, gw_ip)
            self._send_json(result)

        elif path == "/api/port/toggle":
            port_name = str(params.get("port", ""))
            with state_lock:
                current = CURRENT_STATE["ports"].get(port_name.lower(), False)
            result = set_port(port_name, not current, gw_ip)
            self._send_json(result)

        elif path == "/api/port/refresh":
            t = threading.Thread(
                target=refresh_all_ports, args=(gw_ip,), daemon=True
            )
            t.start()
            self._send_json({"success": True, "message": "Refresh started"})

        elif path == "/api/sensor/query":
            s_ip   = params.get("ip", GATEWAY_IP)
            s_port = int(params.get("port", DEFAULT_PORT))
            s_addr = params.get("addr", DEFAULT_ADDR)
            if isinstance(s_addr, str):
                try:
                    s_addr = int(s_addr, 16) if ("0x" in s_addr.lower() or "ff" in s_addr.lower()) else int(s_addr)
                except Exception:
                    s_addr = DEFAULT_ADDR
            result = query_sensor(ip=s_ip, port=s_port, slave_addr=s_addr)
            self._send_json(result)

        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
def run(web_port=8088):
    t = threading.Thread(target=refresh_all_ports, daemon=True)
    t.start()

    server = ThreadingHTTPServer(("0.0.0.0", web_port), GatewayHandler)
    print("+----------------------------------------------+")
    print("|  CJI Gateway Unified Controller              |")
    print(f"|  Dashboard -> http://localhost:{web_port}/        |")
    print(f"|  Gateway   -> {GATEWAY_IP} (admin:admin)   |")
    print("+----------------------------------------------+")
    server.serve_forever()

if __name__ == "__main__":
    web_port = 8088
    if len(sys.argv) > 1:
        web_port = int(sys.argv[1])
    run(web_port)
