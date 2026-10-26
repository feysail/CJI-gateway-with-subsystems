# CJI Gateway Unified Controller (RGB + Port Manager)

This directory combines the **RGB/RGBW Controller** with the **GPIO Port Controller** for the **CJI WMD-7621U** gateway into a single unified dashboard and backend.

---

## Architecture Overview

```
d:\cig_getway\cji_gateway\
├── server.py              # Unified Python backend (HTTP + Socket + MTK GPIO)
├── start_dashboard.bat    # 1-click Windows launcher (Port 8088)
├── README.md              # Documentation
└── static\                # Frontend assets
    ├── index.html         # Modern Glassmorphic Dashboard with Tabs
    ├── style.css          # Premium Dark Theme CSS
    └── app.js             # Dual RGB + Real-time GPIO state synchronization
```

---

## Pinout & Protocol Reference

### 1. GPIO Port Mapping (`apply.cgi` with `admin:admin`)
- **Port 1** → GPIO 13 (High = ON, Low = OFF)
- **Port 2** → GPIO 14 (High = ON, Low = OFF)
- **Port 3** → GPIO 15 (High = ON, Low = OFF)
- **RJ45** → GPIO 0 (**Inverted Logic**: Low 0 = ON, High 1 = OFF)
- **Relay** → GPIO 21 (High = ON, Low = OFF)

Control Command:
```sh
mtk_gpio -d <pin> 1 && mtk_gpio -w <pin> <val> && mtk_gpio -r <pin>
```

### 3. Environmental Sensor Modbus RTU (TCP Socket `192.168.11.1:5021` / `5022`)
- **Protocol**: Modbus RTU over TCP Bridge (FC `0x03` Read Holding/Input Registers)
- **Registers**: 22 continuous 16-bit registers (0x0001 - 0x0016)
- **Key Parameters**:
  - `0x0001`: Ambient Noise (dB = Raw / 10.0)
  - `0x0007`: PM2.5 (µg/m³)
  - `0x0008`: PM10 (µg/m³)
  - `0x0009`: Air Temperature (°C = Raw / 100.0 - 40.0)
  - `0x000A`: Relative Humidity (%RH = Raw / 100.0)
  - `0x000B`: Barometric Pressure (hPa = Raw / 10.0)
  - `0x000C`: Wind Speed (m/s = Raw / 100.0)
  - `0x000D`: Wind Direction (deg = Raw / 10.0)
  - `0x000E`: Continuous Rainfall (mm = Raw / 10.0)
  - `0x000F`: Solar Radiation (W/m²)
  - `0x0010`: Illuminance (Lux = Raw * 10)
  - `0x0011`: UV Index
  - `0x0012`: Carbon Dioxide CO₂ (ppm)
  - `0x0013`: Negative Oxygen Ions (ions/cm³ = Raw * 10)

---

## How to Run

1. Make sure no other process is holding port 8088.
2. Double click [start_dashboard.bat](file:///d:/cig_getway/cji_gateway/start_dashboard.bat) or run:
   ```powershell
   python d:\cig_getway\cji_gateway\server.py 8088
   ```
3. Open your browser:
   **http://localhost:8088/**

---

## API Endpoints

- `GET /api/status` — Returns live RGB, all port states, and sensor telemetry
- `GET /api/sensor?port=5021&addr=0xFF&demo=0` — Queries environmental sensor (supports `demo=1` for simulated testing)
- `GET /api/sensor/history` — Returns recent cached telemetry readings
- `POST /api/sensor/query` — `{"ip": "192.168.11.1", "port": 5021, "addr": "0xFF", "demo": false}`
- `POST /api/port/set` — `{"port": "1", "state": true}` (or "2", "3", "relay")
- `POST /api/port/toggle` — `{"port": "1"}`
- `POST /api/port/refresh` — Triggers background query of all GPIO pins
- `POST /api/rgb` — Sets RGBW color (`r`, `g`, `b`, `w`, `mode`)
- `POST /api/channel` — Sets individual channel (`channel`, `value`)
- `POST /api/effect` — Triggers animations (`jump7`, `rainbow`, `strobe`)
- `POST /api/raw` — Sends custom raw hex packet
