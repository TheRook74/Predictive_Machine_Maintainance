from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio
import json
import numpy as np
import time
from collections import defaultdict
from scipy import signal

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------- Configuration ----------------
BASELINE_SAMPLES_NEEDED = 3000
EMA_ALPHA = 0.2; BROADCAST_HZ = 15.0; BROADCAST_INTERVAL = 1.0 / BROADCAST_HZ
MAX_RAW = 4000; MAX_SMOOTH_HISTORY = 400; SAMPLE_RATE = 100.0
FFT_N = 256
STATUS_CHANGE_THRESHOLD = 3

# --- State Dictionaries ---
machines = defaultdict(lambda: {
    "x_buffer": [], "y_buffer": [], "z_buffer": [], "x_smooth": [], "y_smooth": [], "z_smooth": [],
    "baseline_x_buffer": [], "baseline_y_buffer": [], "baseline_z_buffer": [], "status": "idle",
    "baseline_state": "needed", "status_counter": 0, "last_potential_status": "idle", "command_queue": [],
    "baseline_dc_offset_x": 0.0, "baseline_dc_offset_y": 0.0, "baseline_dc_offset_z": 0.0,
    "baseline_peak_db_mean": None, "baseline_peak_db_std": None,
    "fft_freqs": None, "fft_x_db": None, "fft_y_db": None, "fft_z_db": None,
})
# ✅ FIX 1: Add a global dictionary to hold the latest environment data
environment_data = {"temperature": None, "humidity": None}
dashboard_clients = set(); _last_broadcast = 0.0

# (Helpers to_serializable, apply_ema_and_store are unchanged)
def to_serializable(obj):
    if isinstance(obj, np.ndarray): return obj.tolist()
    if isinstance(obj, (np.floating, np.float32, np.float64)): return float(obj)
    if isinstance(obj, (np.integer, np.int32, np.int64, np.int_)): return int(obj)
    if isinstance(obj, list): return [to_serializable(x) for x in obj]
    return obj
def apply_ema_and_store(machine_id, new_x_list, new_y_list, new_z_list):
    machine = machines[machine_id]
    machine["x_buffer"].extend([float(v) for v in new_x_list]); machine["y_buffer"].extend([float(v) for v in new_y_list]); machine["z_buffer"].extend([float(v) for v in new_z_list])
    machine["x_buffer"], machine["y_buffer"], machine["z_buffer"] = machine["x_buffer"][-MAX_RAW:], machine["y_buffer"][-MAX_RAW:], machine["z_buffer"][-MAX_RAW:]
    for nx, ny, nz in zip(new_x_list, new_y_list, new_z_list):
        if not machine["x_smooth"]: machine["x_smooth"].append(float(nx))
        else: machine["x_smooth"].append(machine["x_smooth"][-1] + EMA_ALPHA * (float(nx) - machine["x_smooth"][-1]))
        if not machine["y_smooth"]: machine["y_smooth"].append(float(ny))
        else: machine["y_smooth"].append(machine["y_smooth"][-1] + EMA_ALPHA * (float(ny) - machine["y_smooth"][-1]))
        if not machine["z_smooth"]: machine["z_smooth"].append(float(nz))
        else: machine["z_smooth"].append(machine["z_smooth"][-1] + EMA_ALPHA * (float(nz) - machine["z_smooth"][-1]))
    H = MAX_SMOOTH_HISTORY
    machine["x_smooth"], machine["y_smooth"], machine["z_smooth"] = machine["x_smooth"][-H:], machine["y_smooth"][-H:], machine["z_smooth"][-H:]

def axis_psd_db_welch(axis_arr, dc_offset):
    a = axis_arr - dc_offset
    freqs, psd = signal.welch(a, fs=SAMPLE_RATE, nperseg=FFT_N)
    psd_db = 10 * np.log10(psd + 1e-12)
    return freqs, psd_db

def calculate_static_baseline(machine_id):
    machine = machines[machine_id]
    print(f"Calculating robust baseline for {machine_id} using {len(machine['baseline_x_buffer'])} samples...")
    machine["baseline_dc_offset_x"] = np.mean(machine["baseline_x_buffer"])
    machine["baseline_dc_offset_y"] = np.mean(machine["baseline_y_buffer"])
    machine["baseline_dc_offset_z"] = np.mean(machine["baseline_z_buffer"])
    print(f"  - DC Offsets (X,Y,Z): {machine['baseline_dc_offset_x']:.2f}, {machine['baseline_dc_offset_y']:.2f}, {machine['baseline_dc_offset_z']:.2f}")

    _, x_db = axis_psd_db_welch(np.array(machine["baseline_x_buffer"]), machine["baseline_dc_offset_x"])
    _, y_db = axis_psd_db_welch(np.array(machine["baseline_y_buffer"]), machine["baseline_dc_offset_y"])
    _, z_db = axis_psd_db_welch(np.array(machine["baseline_z_buffer"]), machine["baseline_dc_offset_z"])

    baseline_peaks = []
    num_chunks = len(machine['baseline_x_buffer']) // FFT_N
    for i in range(num_chunks):
        start = i * FFT_N
        end = start + FFT_N
        if end > len(machine['baseline_x_buffer']): break
        _, x_db_chunk = axis_psd_db_welch(np.array(machine['baseline_x_buffer'][start:end]), machine["baseline_dc_offset_x"])
        _, y_db_chunk = axis_psd_db_welch(np.array(machine['baseline_y_buffer'][start:end]), machine["baseline_dc_offset_y"])
        _, z_db_chunk = axis_psd_db_welch(np.array(machine['baseline_z_buffer'][start:end]), machine["baseline_dc_offset_z"])
        max_peak = max(np.max(x_db_chunk, initial=-200), np.max(y_db_chunk, initial=-200), np.max(z_db_chunk, initial=-200))
        baseline_peaks.append(max_peak)

    machine["baseline_peak_db_mean"] = np.mean(baseline_peaks)
    machine["baseline_peak_db_std"] = np.std(baseline_peaks)
    if machine["baseline_peak_db_std"] < 0.5: machine["baseline_peak_db_std"] = 0.5

    print(f"  - Learned Peak dB Stats: Mean={machine['baseline_peak_db_mean']:.2f}, StdDev={machine['baseline_peak_db_std']:.2f}")

    machine["baseline_x_buffer"], machine["baseline_y_buffer"], machine["baseline_z_buffer"] = [], [], []
    machine["baseline_state"] = "ready"; machine["status"] = "active"
    print(f"Baseline for {machine_id} is ready. Monitoring active.")

def compute_fft_and_compare(machine_id):
    machine = machines[machine_id]
    if machine.get("baseline_peak_db_mean") is None: return
    if len(machine["x_buffer"]) < FFT_N: return

    x = np.array(machine["x_buffer"][-FFT_N:], dtype=float)
    y = np.array(machine["y_buffer"][-FFT_N:], dtype=float)
    z = np.array(machine["z_buffer"][-FFT_N:], dtype=float)

    freqs, x_db = axis_psd_db_welch(x, machine["baseline_dc_offset_x"])
    _, y_db = axis_psd_db_welch(y, machine["baseline_dc_offset_y"])
    _, z_db = axis_psd_db_welch(z, machine["baseline_dc_offset_z"])

    machine["fft_freqs"], machine["fft_x_db"], machine["fft_y_db"], machine["fft_z_db"] = freqs, x_db, y_db, z_db
    max_peak_db = max(np.max(x_db, initial=-200), np.max(y_db, initial=-200), np.max(z_db, initial=-200))
    
    CRITICAL_SIGMA = 7.0; WARNING_SIGMA  = 4.0
    mean = machine["baseline_peak_db_mean"]
    std  = machine["baseline_peak_db_std"]
    critical_threshold = mean + CRITICAL_SIGMA * std
    warning_threshold  = mean + WARNING_SIGMA * std

    print(f"Peak dB: {max_peak_db:5.1f} [Warn > {warning_threshold:.1f}] [Crit > {critical_threshold:.1f}]")

    potential_status = "active"
    if max_peak_db > critical_threshold: potential_status = "critical"
    elif max_peak_db > warning_threshold: potential_status = "warning"
        
    if potential_status == machine["last_potential_status"]: machine["status_counter"] += 1
    else: machine["last_potential_status"], machine["status_counter"] = potential_status, 1
    if machine["status_counter"] >= STATUS_CHANGE_THRESHOLD and machine["status"] != potential_status:
        previous_status = machine["status"]; machine["status"] = potential_status
        print(f"Status for {machine_id} changed from '{previous_status}' to '{machine['status']}'. Queueing command.")
        command = None
        if machine["status"] == "active": command = {"action": "set_leds", "red": "off", "yellow": "off"}
        elif machine["status"] == "warning": command = {"action": "set_leds", "red": "off", "yellow": "blink"}
        elif machine["status"] == "critical": command = {"action": "set_leds", "red": "on", "yellow": "off"}
        if command: machine["command_queue"].append(command)

# ----------------- Routes & WebSockets -----------------
def prepare_snapshot():
    snapshot = {}
    for machine_id, machine in machines.items():
        freqs = machine.get("fft_freqs")
        if freqs is not None:
            max_bins = min(len(freqs), 128)
            freqs_send, x_send, y_send, z_send = freqs[:max_bins].tolist(), machine["fft_x_db"][:max_bins].tolist(), machine["fft_y_db"][:max_bins].tolist(), machine["fft_z_db"][:max_bins].tolist()
        else: freqs_send, x_send, y_send, z_send = [], [], [], []
        snapshot[machine_id] = {"status": machine["status"], "x": to_serializable(machine["x_smooth"][-MAX_SMOOTH_HISTORY:]), "y": to_serializable(machine["y_smooth"][-MAX_SMOOTH_HISTORY:]), "z": to_serializable(machine["z_smooth"][-MAX_SMOOTH_HISTORY:]), "fft_freqs": freqs_send, "fft_x_db": x_send, "fft_y_db": y_send, "fft_z_db": z_send}
    
    # ✅ FIX 2: Merge the environment data into the snapshot payload before sending.
    # The '|' operator merges the two dictionaries.
    return snapshot | environment_data

async def broadcast_dashboard():
    payload = json.dumps(prepare_snapshot())
    for client in list(dashboard_clients):
        try: await client.send_text(payload)
        except Exception:
            try: dashboard_clients.remove(client)
            except KeyError: pass

@app.get("/")
async def dashboard(request: Request): return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws/esp")
async def websocket_esp(websocket: WebSocket):
    await websocket.accept(); print("ESP32 connected"); machine_id = None; global _last_broadcast
    try:
        while True:
            data = json.loads(await websocket.receive_text()); machine_id = data.get("machine_id") or data.get("id")
            msg_type = data.get("type", "data")

            # Added for handling the pong message...
            if msg_type == "ping":
                print("Recieved application Level Ping from ESP-32")
                continue

            if not machine_id: continue
            machine = machines[machine_id]; 
            
            if msg_type == "configure":
                x_vals, y_vals, z_vals = data.get("x", []), data.get("y", []), data.get("z", [])
                machine["baseline_state"] = "collecting"; machine["status"] = "configuring"
                machine["baseline_x_buffer"].extend(x_vals); machine["baseline_y_buffer"].extend(y_vals); machine["baseline_z_buffer"].extend(z_vals)
                print(f"Received {len(x_vals)} baseline samples from {machine_id}. Total: {len(machine['baseline_x_buffer'])}")
                if len(machine["baseline_x_buffer"]) >= BASELINE_SAMPLES_NEEDED: calculate_static_baseline(machine_id)
            
            elif msg_type == "data" and machine["baseline_state"] == "ready":
                x_vals, y_vals, z_vals = data.get("x", []), data.get("y", []), data.get("z", [])
                apply_ema_and_store(machine_id, x_vals, y_vals, z_vals); compute_fft_and_compare(machine_id)

            # ✅ FIX 1: Add logic to handle incoming DHT sensor data
            elif msg_type == "dht" or msg_type == "baseline_dht":
                temp = data.get("temperature")
                hum = data.get("humidity")
                if temp is not None and hum is not None:
                    environment_data["temperature"] = temp
                    environment_data["humidity"] = hum
                    print(f"Received Environment Data: Temp={temp}°C, Humidity={hum}%")

            if machine["command_queue"]:
                cmd = machine["command_queue"].pop(0)
                try: await websocket.send_text(json.dumps({"command": cmd})); print(f"Sent command to {machine_id}: {cmd}")
                except Exception as e: print("Failed to send command:", e)
            
            now = time.time()
            if now - _last_broadcast >= BROADCAST_INTERVAL: await broadcast_dashboard(); _last_broadcast = now
            
    except WebSocketDisconnect: print(f"{machine_id} disconnected")
    except Exception as e: print("Exception in websocket_esp:", e)

@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    await websocket.accept(); dashboard_clients.add(websocket); print("Dashboard connected (clients):", len(dashboard_clients))
    try:
        # Send initial data immediately on connect
        await broadcast_dashboard()
        while True:
            # Continue sending updates, but less frequently
            await asyncio.sleep(5)
            await broadcast_dashboard()
    except WebSocketDisconnect:
        dashboard_clients.remove(websocket)
        print("Dashboard disconnected (clients):", len(dashboard_clients))

@app.post("/command")
async def send_command(request: Request):
    data = await request.json(); machine_id, command = data.get("id"), data.get("command")
    if machine_id in machines:
        # Simplified command handling for this example
        if command == "stop":
            # This is a placeholder; define what "stop" means for your system.
            # Maybe queue a specific LED command or change state.
            machines[machine_id]["command_queue"].append({"action": "set_leds", "red": "on", "yellow": "off"})
        elif command == "restart":
            # Placeholder; you might reset the baseline state, for example.
            machines[machine_id]["baseline_state"] = "needed"
            machines[machine_id]["status"] = "idle"
        return {"status": "ok", "message": f"Command '{command}' queued for {machine_id}"}
    return {"status": "machine not found"}