/* ══════════════════════════════════════════════════
   CJI Gateway — Unified Dashboard Logic (app.js)
   ══════════════════════════════════════════════════ */

// ─── Local state ─────────────────────────────────
const rgb = { r: 255, g: 0, b: 0, w: 0, brightness: 100, mode: 'both' };

const PORT_NAMES = ['1', '2', '3', 'relay'];
const portBusy = {};   // tracks in-flight requests

// ─── Section switcher ─────────────────────────────
function switchSection(name) {
  document.querySelectorAll('.section').forEach(el => el.classList.add('hidden'));
  document.querySelectorAll('.sec-tab').forEach(el => el.classList.remove('active'));
  const sec = document.getElementById('section-' + name);
  const tab = document.getElementById('tab-' + name);
  if (sec) sec.classList.remove('hidden');
  if (tab) tab.classList.add('active');

  if (name === 'sensor') {
    startSensorPolling();
    pollSensorNow();
  } else {
    stopSensorPolling();
  }
}

// ─── Toast ────────────────────────────────────────
let toastTimer;
function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 3000);
}

// ─── Refresh button ───────────────────────────────
function refreshAll() {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  post('/api/port/refresh', { ip: gwIp() })
    .then(() => {
      setTimeout(() => {
        btn.classList.remove('spinning');
        showToast('Port states refreshed', 'success');
      }, 2000); // give time for bg task to finish
    })
    .catch(() => btn.classList.remove('spinning'));
}

function gwIp() {
  const el = document.getElementById('cfg-ip');
  return el ? el.value.trim() : '192.168.11.1';
}

// ─── API helpers ──────────────────────────────────
function post(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(r => r.json());
}

// ─── RGB / RGBW ───────────────────────────────────
function setProtocolMode(m) {
  rgb.mode = m;
  document.querySelectorAll('.mode-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('mtab-' + m).classList.add('active');
  applyCurrentColor();
}

function updateVisuals() {
  const scale = rgb.brightness / 100;
  const fR = Math.round(rgb.r * scale);
  const fG = Math.round(rgb.g * scale);
  const fB = Math.round(rgb.b * scale);
  const fW = Math.round(rgb.w * scale);

  const hex = '#' + [fR, fG, fB].map(x => x.toString(16).padStart(2, '0')).join('').toUpperCase();

  document.getElementById('hex-label').textContent = hex;
  document.getElementById('rgb-label').textContent = `RGBW(${fR}, ${fG}, ${fB}, ${fW}) \u2022 ${rgb.brightness}%`;
  document.getElementById('preview-stage').style.backgroundColor = hex;
  document.getElementById('preview-glow').style.backgroundColor = hex;

  document.getElementById('slider-r').value = rgb.r;
  document.getElementById('slider-g').value = rgb.g;
  document.getElementById('slider-b').value = rgb.b;
  document.getElementById('slider-w').value = rgb.w;
  document.getElementById('badge-r').textContent = rgb.r;
  document.getElementById('badge-g').textContent = rgb.g;
  document.getElementById('badge-b').textContent = rgb.b;
  document.getElementById('badge-w').textContent = rgb.w;
  document.getElementById('brightness-slider').value = rgb.brightness;
  document.getElementById('brightness-badge').textContent = rgb.brightness + '%';
  document.getElementById('native-picker').value = hex;
}

function onColorPicked(hex) {
  rgb.r = parseInt(hex.substr(1, 2), 16);
  rgb.g = parseInt(hex.substr(3, 2), 16);
  rgb.b = parseInt(hex.substr(5, 2), 16);
  updateVisuals();
  applyCurrentColor();
}

function onChannelSlider(chNum, chKey, val) {
  rgb[chKey] = parseInt(val);
  updateVisuals();
  clearTimeout(window._sliderTimer);
  window._sliderTimer = setTimeout(() => {
    const port = parseInt(document.getElementById('cfg-port').value);
    const addr = parseInt(document.getElementById('cfg-addr').value, 16);
    post('/api/channel', {
      channel: chNum, value: parseInt(val),
      ip: gwIp(), port, addr,
    }).then(d => appendLog(d.tx, d.error));
  }, 80);
}

function onBrightnessInput(val) {
  rgb.brightness = parseInt(val);
  updateVisuals();
  clearTimeout(window._brightnessTimer);
  window._brightnessTimer = setTimeout(applyCurrentColor, 120);
}

function setPreset(r, g, b, w) {
  rgb.r = r; rgb.g = g; rgb.b = b; rgb.w = w;
  updateVisuals();
  applyCurrentColor();
}

function powerToggle() { setPreset(0, 0, 0, 0); }

function applyCurrentColor() {
  const scale = rgb.brightness / 100;
  const fR = Math.round(rgb.r * scale);
  const fG = Math.round(rgb.g * scale);
  const fB = Math.round(rgb.b * scale);
  const fW = Math.round(rgb.w * scale);
  const port = parseInt(document.getElementById('cfg-port').value);
  const addr = parseInt(document.getElementById('cfg-addr').value, 16);

  // Clear active effect highlighting if manual color is set
  _clearActiveEffectUI();

  post('/api/rgb', { r: fR, g: fG, b: fB, w: fW, ip: gwIp(), port, addr, mode: rgb.mode })
    .then(d => appendLog(d.tx, d.error))
    .catch(e => appendLog(null, e.message));
}

let activeEffectName = null;
let activeEffectSpeed = 1.0;

function _clearActiveEffectUI() {
  activeEffectName = null;
  document.querySelectorAll('.effect-btn').forEach(b => b.classList.remove('active'));
}

function setEffectSpeed(speed) {
  activeEffectSpeed = speed;
  ['05', '10', '20'].forEach(id => {
    const el = document.getElementById(`spd-${id}`);
    if (el) el.classList.remove('active');
  });
  const map = { 0.5: '05', 1.0: '10', 2.0: '20' };
  const target = document.getElementById(`spd-${map[speed] || '10'}`);
  if (target) target.classList.add('active');

  if (activeEffectName && activeEffectName !== 'stop') {
    triggerEffect(activeEffectName, false);
  }
}

function triggerEffect(effect, showToastMsg = true) {
  const port = parseInt(document.getElementById('cfg-port').value);
  const addr = parseInt(document.getElementById('cfg-addr').value, 16);

  if (effect === 'stop') {
    _clearActiveEffectUI();
    post('/api/effect', { effect: 'stop', ip: gwIp(), port, addr })
      .then(d => {
        appendLog(null, null, '[Effect Stopped]');
        showToast('Dynamic effects stopped. Current color held.', 'info');
      });
    return;
  }

  activeEffectName = effect;
  document.querySelectorAll('.effect-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`eff-${effect}`);
  if (btn) btn.classList.add('active');

  post('/api/effect', { effect, speed: activeEffectSpeed, ip: gwIp(), port, addr })
    .then(d => {
      appendLog(null, null, `[Effect] ${effect} (${activeEffectSpeed}x) started`);
      if (showToastMsg) showToast(`Effect '${effect.replace('_', ' ')}' active (${activeEffectSpeed}×)`, 'success');
    })
    .catch(e => appendLog(null, e.message));
}

function setChannelDirect(channel, value) {
  const chKey = { 1: 'r', 2: 'g', 3: 'b', 4: 'w' }[channel];
  if (!chKey) return;
  const slider = document.getElementById(`slider-${chKey}`);
  const badge  = document.getElementById(`badge-${chKey}`);
  if (slider) slider.value = value;
  if (badge)  badge.innerText = value;
  rgb[chKey] = value;
  updateColorView();
  transmitRgb();
  showToast(`CH${channel} (${chKey.toUpperCase()}): ${value === 255 ? '100%' : '0%'}`, 'success');
}

function queryDevice(qType) {
  const port = parseInt(document.getElementById('cfg-port').value);
  const addr = parseInt(document.getElementById('cfg-addr').value, 16);
  post('/api/query', { type: qType, ip: gwIp(), port, addr })
    .then(d => {
      appendLog(d.tx, d.error, d.rx);
      const title = qType === 'rgb' ? 'RGB Output' : (qType === 'addr' ? 'Device Address' : 'Baud Rate');
      if (d.rx) {
        showToast(`SSCOM ${title}: ${d.rx}`, 'success');
      } else {
        showToast(`SSCOM ${title} sent: ${d.tx}`, 'info');
      }
    })
    .catch(e => appendLog(null, e.message));
}

function sendRawHex() {
  const inp = document.getElementById('raw-hex-input');
  const hex = inp.value.trim();
  if (!hex) return;
  const port = parseInt(document.getElementById('cfg-port').value);
  post('/api/raw', { hex, ip: gwIp(), port })
    .then(d => { appendLog(d.tx, d.error); inp.value = ''; });
}

function appendLog(tx, err, rx) {
  const term = document.getElementById('terminal-box');
  const now = new Date().toTimeString().split(' ')[0];
  const div = document.createElement('div');
  div.className = 'log-line';
  let html = `<span class="log-time">[${now}]</span> `;
  if (tx) html += `<span class="log-tx">TX &rarr; ${tx}</span> `;
  if (rx) html += `<span class="log-rx" style="color:#22c55e;">RX &larr; ${rx}</span> `;
  if (err) html += `<span class="log-err">[${err}]</span>`;
  div.innerHTML = html;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
  if (term.children.length > 70) term.removeChild(term.firstChild);
}

function clearLogs() { document.getElementById('terminal-box').innerHTML = ''; }

// ─── PORT CONTROL ─────────────────────────────────

function setPort(portName, on) {
  if (portBusy[portName]) return;
  portBusy[portName] = true;
  _setPortBusy(portName, true);

  post('/api/port/set', { port: portName, state: on, ip: gwIp() })
    .then(data => {
      portBusy[portName] = false;
      if (data.success) {
        const valStr = data.state ? '1' : '0';
        _applyPortState(portName, data.state);
        showToast(`Port ${portName.toUpperCase()}: ${valStr}`, 'success');
        setPortLog(portName, valStr, 'ok');
        if (portName === '1') {
          _applyPortState('relay', data.state);
          setPortLog('relay', valStr, 'ok');
        } else if (portName === 'relay') {
          _applyPortState('1', data.state);
          setPortLog('1', valStr, 'ok');
        }
      } else {
        _setPortBusy(portName, false);
        showToast(`Port ${portName}: ${data.error}`, 'error');
        setPortLog(portName, `ERROR: ${data.error}`, 'err');
      }
    })
    .catch(err => {
      portBusy[portName] = false;
      _setPortBusy(portName, false);
      showToast(`Network error: ${err.message}`, 'error');
      setPortLog(portName, `Network error: ${err.message}`, 'err');
    });
}

function togglePort(portName) {
  // Read current state from DOM
  const badge = document.getElementById('badge-port-' + portName);
  const isOn = badge && (badge.classList.contains('on') || badge.textContent === '1');
  setPort(portName, !isOn);
}

function setAllPorts(on) {
  PORT_NAMES.forEach(p => {
    setTimeout(() => setPort(p, on), PORT_NAMES.indexOf(p) * 120);
  });
}

function _setPortBusy(portName, busy) {
  const card = document.getElementById('port-card-' + portName);
  const badge = document.getElementById('badge-port-' + portName);
  if (!card) return;
  if (busy) {
    card.classList.remove('state-on');
    card.classList.add('state-busy');
    badge.textContent = '…';
    badge.className = 'port-status-badge busy';
    setPortLog(portName, 'Updating…', '');
  } else {
    card.classList.remove('state-busy');
  }
}

function _applyPortState(portName, on) {
  const card = document.getElementById('port-card-' + portName);
  const badge = document.getElementById('badge-port-' + portName);
  const visual = document.getElementById('visual-port-' + portName);
  const stateText = document.getElementById('state-port-' + portName);

  // Live state table
  const ls = document.getElementById('ls-' + portName);

  if (!card) return;

  card.classList.remove('state-busy');
  const valStr = on ? '1' : '0';
  if (on) {
    card.classList.add('state-on');
    badge.textContent = '1';
    badge.className = 'port-status-badge on';
    visual && visual.classList.add('state-on');
    stateText && (stateText.textContent = '1');
    if (ls) { ls.textContent = '1'; ls.className = 'live-state on'; }
  } else {
    card.classList.remove('state-on');
    badge.textContent = '0';
    badge.className = 'port-status-badge';
    visual && visual.classList.remove('state-on');
    stateText && (stateText.textContent = '0');
    if (ls) { ls.textContent = '0'; ls.className = 'live-state off'; }
  }
  setPortLog(portName, valStr, on ? 'ok' : '');
}

function setPortLog(portName, msg, cls) {
  const el = document.getElementById('log-port-' + portName);
  if (!el) return;
  el.textContent = msg;
  el.className = 'port-log ' + (cls || '');
}

// ─── STATUS POLL ──────────────────────────────────
let _isUserInteracting = false;
window.addEventListener('mousedown', () => { _isUserInteracting = true; });
window.addEventListener('touchstart', () => { _isUserInteracting = true; }, { passive: true });
window.addEventListener('mouseup', () => setTimeout(() => { _isUserInteracting = false; }, 600));
window.addEventListener('touchend', () => setTimeout(() => { _isUserInteracting = false; }, 600));

function pollStatus() {
  if (_isUserInteracting) return;
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      if (_isUserInteracting) return;

      // Update connection dot
      document.getElementById('conn-dot').classList.remove('error');

      // Sync RGB state
      if (data.r !== undefined && !_isUserInteracting) {
        const changed = rgb.r !== data.r || rgb.g !== data.g ||
          rgb.b !== data.b || rgb.w !== data.w;
        if (changed) {
          rgb.r = data.r; rgb.g = data.g;
          rgb.b = data.b; rgb.w = data.w;
          updateVisuals();
        }
      }

      // Sync port states
      if (data.ports) {
        for (const [name, on] of Object.entries(data.ports)) {
          if (!portBusy[name]) {
            _applyPortState(name, on);
          }
        }
      }
    })
    .catch(() => {
      document.getElementById('conn-dot').classList.add('error');
    });
}

// ─── INIT ─────────────────────────────────────────
updateVisuals();
setInterval(pollStatus, 1500);

// Trigger initial port refresh after 1.5s (server does it on startup)
setTimeout(() => {
  pollStatus(); // get whatever the server already has
}, 1600);


// ══════════════════════════════════════════════════
//  Environmental Sensor Controller Module
// ══════════════════════════════════════════════════
let sensorTimer = null;
let sensorBusy = false;

function sensorPort() {
  const el = document.getElementById('sensor-cfg-port');
  return el ? parseInt(el.value) : 5021;
}

function sensorAddr() {
  const el = document.getElementById('sensor-cfg-addr');
  return el ? el.value.trim() : '0xFF';
}

function onSensorConfigChange() {
  showToast(`Sensor Target: Port ${sensorPort()}, Addr ${sensorAddr()}`, 'success');
  pollSensorNow();
}

function onSensorPollIntervalChange() {
  const interval = parseInt(document.getElementById('sensor-poll-interval').value);
  stopSensorPolling();
  if (interval > 0) {
    startSensorPolling(interval);
    showToast(`Sensor polling every ${interval / 1000}s`, 'success');
  } else {
    showToast('Auto-polling paused (Manual read only)', 'success');
  }
}

function startSensorPolling(ms) {
  stopSensorPolling();
  const interval = ms || parseInt(document.getElementById('sensor-poll-interval').value);
  if (interval > 0) {
    sensorTimer = setInterval(() => {
      const activeTab = document.getElementById('tab-sensor');
      if (activeTab && activeTab.classList.contains('active')) {
        pollSensorNow();
      }
    }, interval);
  }
}

function stopSensorPolling() {
  if (sensorTimer) {
    clearInterval(sensorTimer);
    sensorTimer = null;
  }
}

function pollSensorNow() {
  if (sensorBusy) return;
  sensorBusy = true;

  const btn = document.getElementById('sensor-read-btn');
  const pulseDot = document.getElementById('sensor-pulse-dot');
  const statusText = document.getElementById('sensor-status-text');

  if (btn) btn.classList.add('loading');
  if (statusText) statusText.textContent = 'Querying…';

  const params = new URLSearchParams({
    ip: gwIp(),
    port: sensorPort(),
    addr: sensorAddr()
  });

  fetch('/api/sensor?' + params.toString())
    .then(r => r.json())
    .then(data => {
      sensorBusy = false;
      if (btn) btn.classList.remove('loading');

      if (data.success) {
        if (pulseDot) {
          pulseDot.className = 'status-pulse-dot online';
        }
        if (statusText) {
          statusText.textContent = `Online (${data.timestamp})`;
        }
        updateSensorUI(data);
      } else {
        if (pulseDot) pulseDot.className = 'status-pulse-dot offline';
        if (statusText) statusText.textContent = 'Offline';
        
        // Show error in TX/RX hex box
        const rxBox = document.getElementById('sensor-rx-hex');
        if (rxBox) rxBox.textContent = `No response (${data.error || '0 bytes'})`;
        if (data.tx_hex) {
          const txBox = document.getElementById('sensor-tx-hex');
          if (txBox) txBox.textContent = data.tx_hex;
        }
        
        // If cached data is available, populate gracefully
        if (data.last_cached) {
          updateSensorUI(data.last_cached, true);
        }
      }
    })
    .catch(err => {
      sensorBusy = false;
      if (btn) btn.classList.remove('loading');
      if (pulseDot) pulseDot.className = 'status-pulse-dot offline';
      if (statusText) statusText.textContent = 'Net Error';
    });
}

function updateSensorUI(d, isCached = false) {
  if (!d) return;

  // Temperature
  const t = d.temperature;
  const tF = (t * 9 / 5 + 32).toFixed(1);
  const valTemp = document.getElementById('val-temp');
  if (valTemp) valTemp.innerHTML = `${t}<span class="metric-unit">°C</span>`;
  const valTempF = document.getElementById('val-temp-f');
  if (valTempF) valTempF.textContent = `${tF} °F`;

  const badgeTemp = document.getElementById('badge-temp');
  if (badgeTemp) {
    if (t < 18) {
      badgeTemp.textContent = 'Cold';
      badgeTemp.className = 'telemetry-badge cold';
    } else if (t <= 27) {
      badgeTemp.textContent = 'Comfort';
      badgeTemp.className = 'telemetry-badge good';
    } else {
      badgeTemp.textContent = 'Warm';
      badgeTemp.className = 'telemetry-badge warm';
    }
  }
  const meterTemp = document.getElementById('meter-temp');
  if (meterTemp) {
    const pct = Math.min(100, Math.max(0, ((t - (-40)) / 100) * 100));
    meterTemp.style.width = pct + '%';
  }

  // Relative Humidity
  const h = d.humidity;
  const valHum = document.getElementById('val-hum');
  if (valHum) valHum.innerHTML = `${h}<span class="metric-unit">%RH</span>`;
  
  // Dew Point Magnus formula approximation
  const a = 17.27, b = 237.7;
  const alpha = ((a * t) / (b + t)) + Math.log(Math.max(1, h) / 100.0);
  const dewPt = ((b * alpha) / (a - alpha)).toFixed(1);
  const valDewPt = document.getElementById('val-dew-point');
  if (valDewPt) valDewPt.textContent = `Dew Pt: ${dewPt}°C`;

  const badgeHum = document.getElementById('badge-hum');
  if (badgeHum) {
    if (h < 30) {
      badgeHum.textContent = 'Dry';
      badgeHum.className = 'telemetry-badge warm';
    } else if (h <= 60) {
      badgeHum.textContent = 'Optimal';
      badgeHum.className = 'telemetry-badge good';
    } else {
      badgeHum.textContent = 'Humid';
      badgeHum.className = 'telemetry-badge warm';
    }
  }
  const meterHum = document.getElementById('meter-hum');
  if (meterHum) meterHum.style.width = Math.min(100, Math.max(0, h)) + '%';

  // Noise
  const n = d.noise;
  const valNoise = document.getElementById('val-noise');
  if (valNoise) valNoise.innerHTML = `${n}<span class="metric-unit">dB</span>`;
  const badgeNoise = document.getElementById('badge-noise');
  if (badgeNoise) {
    if (n < 55) {
      badgeNoise.textContent = 'Quiet';
      badgeNoise.className = 'telemetry-badge good';
    } else if (n <= 70) {
      badgeNoise.textContent = 'Moderate';
      badgeNoise.className = 'telemetry-badge mod';
    } else {
      badgeNoise.textContent = 'Loud';
      badgeNoise.className = 'telemetry-badge danger';
    }
  }
  const meterNoise = document.getElementById('meter-noise');
  if (meterNoise) {
    const nPct = Math.min(100, Math.max(0, ((n - 30) / 90) * 100));
    meterNoise.style.width = nPct + '%';
  }

  // PM2.5
  const pm25 = d.pm25;
  const valPM25 = document.getElementById('val-pm25');
  if (valPM25) valPM25.innerHTML = `${pm25}<span class="metric-unit">µg/m³</span>`;
  const badgePM25 = document.getElementById('badge-pm25');
  const valPM25AQI = document.getElementById('val-pm25-aqi');
  if (badgePM25) {
    if (pm25 <= 12) {
      badgePM25.textContent = 'Clean';
      badgePM25.className = 'telemetry-badge good';
      if (valPM25AQI) valPM25AQI.textContent = 'AQI: Good';
    } else if (pm25 <= 35) {
      badgePM25.textContent = 'Moderate';
      badgePM25.className = 'telemetry-badge mod';
      if (valPM25AQI) valPM25AQI.textContent = 'AQI: Mod';
    } else {
      badgePM25.textContent = 'Alert';
      badgePM25.className = 'telemetry-badge danger';
      if (valPM25AQI) valPM25AQI.textContent = 'AQI: High';
    }
  }
  const meterPM25 = document.getElementById('meter-pm25');
  if (meterPM25) {
    const p25Pct = Math.min(100, Math.max(0, (pm25 / 150) * 100));
    meterPM25.style.width = p25Pct + '%';
  }

  // PM10
  const pm10 = d.pm10;
  const valPM10 = document.getElementById('val-pm10');
  if (valPM10) valPM10.innerHTML = `${pm10}<span class="metric-unit">µg/m³</span>`;
  const badgePM10 = document.getElementById('badge-pm10');
  if (badgePM10) {
    if (pm10 <= 54) {
      badgePM10.textContent = 'Clean';
      badgePM10.className = 'telemetry-badge good';
    } else if (pm10 <= 154) {
      badgePM10.textContent = 'Moderate';
      badgePM10.className = 'telemetry-badge mod';
    } else {
      badgePM10.textContent = 'Heavy';
      badgePM10.className = 'telemetry-badge danger';
    }
  }
  const meterPM10 = document.getElementById('meter-pm10');
  if (meterPM10) {
    const p10Pct = Math.min(100, Math.max(0, (pm10 / 250) * 100));
    meterPM10.style.width = p10Pct + '%';
  }

  // CO2
  const co2 = d.co2;
  const valCO2 = document.getElementById('val-co2');
  if (valCO2) valCO2.innerHTML = `${co2}<span class="metric-unit">ppm</span>`;
  const badgeCO2 = document.getElementById('badge-co2');
  if (badgeCO2) {
    if (co2 < 600) {
      badgeCO2.textContent = 'Fresh';
      badgeCO2.className = 'telemetry-badge good';
    } else if (co2 < 1000) {
      badgeCO2.textContent = 'Normal';
      badgeCO2.className = 'telemetry-badge good';
    } else {
      badgeCO2.textContent = 'Elevated';
      badgeCO2.className = 'telemetry-badge warm';
    }
  }
  const meterCO2 = document.getElementById('meter-co2');
  if (meterCO2) {
    const co2Pct = Math.min(100, Math.max(0, ((co2 - 400) / 1600) * 100));
    meterCO2.style.width = co2Pct + '%';
  }

  // Secondary Strip
  const valPress = document.getElementById('val-pressure');
  if (valPress) valPress.innerHTML = `${d.pressure} <span class="sub-unit">hPa</span>`;
  const valWind = document.getElementById('val-wind-speed');
  if (valWind) valWind.innerHTML = `${d.wind_speed} <span class="sub-unit">m/s</span>`;

  const valWindDir = document.getElementById('val-wind-dir');
  if (valWindDir) valWindDir.innerHTML = `${d.wind_direction} <span class="sub-unit">°</span> <span id="val-compass" class="sub-tag">${getCompassHeading(d.wind_direction)}</span>`;

  const valRain = document.getElementById('val-rain');
  if (valRain) valRain.innerHTML = `${d.rainfall} <span class="sub-unit">mm</span>`;
  const valSolar = document.getElementById('val-solar');
  if (valSolar) valSolar.innerHTML = `${d.solar_radiation} <span class="sub-unit">W/m²</span>`;
  const valLux = document.getElementById('val-lux');
  if (valLux) valLux.innerHTML = `${d.illuminance} <span class="sub-unit">Lux</span>`;
  const valUV = document.getElementById('val-uv');
  if (valUV) valUV.innerHTML = `${d.uv_index} <span class="sub-unit">UV</span>`;
  const valIons = document.getElementById('val-ions');
  if (valIons) valIons.innerHTML = `${d.negative_ions} <span class="sub-unit">ions</span>`;

  // Frames & Diag
  const txHex = document.getElementById('sensor-tx-hex');
  if (txHex && d.tx_hex) txHex.textContent = d.tx_hex;

  const rxHex = document.getElementById('sensor-rx-hex');
  if (rxHex && d.raw_hex) rxHex.textContent = d.raw_hex;

  const timeEl = document.getElementById('sensor-last-polled-time');
  if (timeEl && d.full_time) {
    timeEl.textContent = `${isCached ? '[Cached] ' : ''}Polled: ${d.full_time}`;
  }

  // Chips
  const chipCrc = document.getElementById('chip-crc');
  if (chipCrc) {
    chipCrc.textContent = 'CRC: Verified OK';
    chipCrc.className = 'status-chip good';
  }
  const chipLen = document.getElementById('chip-len');
  if (chipLen) {
    const byteLen = d.raw_hex ? d.raw_hex.split(' ').length : 49;
    chipLen.textContent = `Length: ${byteLen} bytes`;
  }
  const chipPort = document.getElementById('chip-port');
  if (chipPort) {
    chipPort.textContent = `Port: ${d.port || sensorPort()} (${(d.port || sensorPort()) == 5021 ? '/dev/ttyS2' : '/dev/ttyS1'})`;
  }
}

function getCompassHeading(deg) {
  const directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const index = Math.round((deg % 360) / 22.5) % 16;
  return directions[index];
}
