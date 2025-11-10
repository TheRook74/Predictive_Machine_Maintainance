from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio
import json
import numpy as np
import time
from collections import defaultdict
from scipy import signal
import os
from dotenv import load_dotenv
import telegram
from fastapi.responses import JSONResponse
import pandas as pd 

# Imports related to InfluxDB
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import datetime

load_dotenv()

# ---------------- InfluxDB Configuration ----------------
# THIS IS YOUR UNIQUE URL FROM THE INFLUXDB CLOUD UI
INFLUXDB_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
# YOUR ORGANIZATION NAME FROM THE CLOUD UI
INFLUXDB_ORG = "IOT Project"
# THE BUCKET YOU CREATED IN THE CLOUD UI
INFLUXDB_BUCKET = "my-bucket"

# Telegram Configuration...
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERT_COOLDOWN_SECONDS = 60

# Initalize telegram bot
bot = telegram.Bot(token=TELEGRAM_TOKEN)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

try:
    influx_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    query_api = influx_client.query_api()
    print("Successfully connected to InfluxDB Cloud.")
except Exception as e:
    print(f"FATAL: Could not connect to InfluxDB. Check credentials. Error: {e}")
    influx_client = None
    write_api = None
    query_api = None

# --- ADD THIS ENTIRE CLASS ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, machine_id: str):
        # The 'accept' call has been moved to the websocket_esp function.
        # This method now only stores the already-accepted connection.
        self.active_connections[machine_id] = websocket

    def disconnect(self, machine_id: str):
        # 1. Remove the active websocket connection
        if machine_id in self.active_connections:
            del self.active_connections[machine_id]
            print(f"Removed connection for '{machine_id}' from the active pool.")

        # 2. Completely delete the application state for that machine.
        #    The defaultdict will create a fresh one if it ever reconnects.
        if machine_id in machines:
            del machines[machine_id]
            print(f"Cleared all application state and data buffers for '{machine_id}'.")

    async def send_command(self, machine_id: str, command: dict):
        if machine_id in self.active_connections:
            websocket = self.active_connections[machine_id]
            try:
                await websocket.send_text(json.dumps({"command": command}))
                print(f"Sent command to {machine_id}: {command}")
            except Exception as e:
                print(f"Failed to send command to {machine_id}: {e}")

# Create a single, global instance of the manager
manager = ConnectionManager()

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
    "fft_freqs": None, "fft_x_db": None, "fft_y_db": None, "fft_z_db": None,"last_alert_time":0
})

async def send_telegram_alert(message: str):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        print(f"Sent Telegram alert: {message}")
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

# FIX 1: Add a global dictionary to hold the latest environment data
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
    
    CRITICAL_SIGMA =10.0; WARNING_SIGMA  = 6.0
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

        # --- ALERTING LOGIC ---
        now = time.time()
        if machine["status"] in ["warning", "critical"]:
            if now - machine.get("last_alert_time", 0) > ALERT_COOLDOWN_SECONDS:
                alert_message = f" ALERT: Machine '{machine_id}' status changed to {machine['status'].upper()}!"
                asyncio.create_task(send_telegram_alert(alert_message))
                machine["last_alert_time"] = now
        # --- END ALERTING LOGIC ---

        command = None
        if machine["status"] == "active": command = {"action": "set_leds", "red": "off", "yellow": "off"}
        elif machine["status"] == "warning": command = {"action": "set_leds", "red": "off", "yellow": "blink"}
        elif machine["status"] == "critical": command = {"action": "set_leds", "red": "on", "yellow": "off"}
        if command: asyncio.create_task(manager.send_command(machine_id, command))

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
    
    # FIX 2: Merge the environment data into the snapshot payload before sending.
    # The '|' operator merges the two dictionaries.
    return snapshot | environment_data

# --- ADD THIS ENTIRE NEW FUNCTION ---
def process_esp_message(data: dict, machine_id: str):
    global _last_broadcast
    msg_type = data.get("type", "data")

    # --- ADD THIS NEW SECTION TO LOG DATA TO INFLUXDB ---
    if write_api and msg_type in ["configure", "data", "dht", "baseline_dht"]:
        try:
            # A "Point" is a single data record for InfluxDB.
            point = Point("machine_data") \
                .tag("machine_id", machine_id) \
                .tag("type", msg_type) \
                .time(datetime.datetime.utcnow()) # Use UTC for consistent timestamps

            # Add simple fields like temperature and humidity directly.
            for key, value in data.items():
                if key in ["temperature", "humidity"]:
                    point.field(key, float(value))

            # For vibration arrays, store statistical aggregates, not the raw array.
            # This is far more efficient for long-term storage.
            if "x" in data and data["x"]:
                point.field("x_mean", np.mean(data["x"]))
                point.field("y_mean", np.mean(data["y"]))
                point.field("z_mean", np.mean(data["z"]))
                point.field("batch_size", len(data["x"]))

            # Write the point to your bucket.
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            # This console log confirms the write was successful.
            print(f"-> Logged '{msg_type}' data for {machine_id} to InfluxDB.")

        except Exception as e:
            print(f"Error writing to InfluxDB: {e}")
    # --- END OF NEW SECTION ---

    if msg_type == "ping":
        print(f"<- Received keep-alive ping from {machine_id}.")
        return

    machine = machines[machine_id]
    
    if msg_type == "configure":
        x_vals, y_vals, z_vals = data.get("x", []), data.get("y", []), data.get("z", [])
        machine["baseline_state"] = "collecting"; machine["status"] = "configuring"
        machine["baseline_x_buffer"].extend(x_vals); machine["baseline_y_buffer"].extend(y_vals); machine["baseline_z_buffer"].extend(z_vals)
        print(f"Received {len(x_vals)} baseline samples from {machine_id}. Total: {len(machine['baseline_x_buffer'])}")
        if len(machine["baseline_x_buffer"]) >= BASELINE_SAMPLES_NEEDED:
            calculate_static_baseline(machine_id)
    
    elif msg_type == "data" and machine["baseline_state"] == "ready":
        x_vals, y_vals, z_vals = data.get("x", []), data.get("y", []), data.get("z", [])
        apply_ema_and_store(machine_id, x_vals, y_vals, z_vals)
        compute_fft_and_compare(machine_id) # This function will now send commands via manager

    elif msg_type in ["dht", "baseline_dht"]:
        temp = data.get("temperature"); hum = data.get("humidity")
        if temp is not None and hum is not None:
            environment_data["temperature"] = temp; environment_data["humidity"] = hum

    # Broadcast to dashboards periodically
    now = time.time()
    if now - _last_broadcast >= BROADCAST_INTERVAL:
        asyncio.create_task(broadcast_dashboard())
        _last_broadcast = now

async def broadcast_dashboard():
    payload = json.dumps(prepare_snapshot())
    for client in list(dashboard_clients):
        try: await client.send_text(payload)
        except Exception:
            try: dashboard_clients.remove(client)
            except KeyError: pass

@app.get("/")
async def dashboard(request: Request): return templates.TemplateResponse("index.html", {"request": request})

# --- REPLACE THE EXISTING /ws/esp FUNCTION WITH THIS ---
# --- REPLACE the existing /ws/esp function with this ---
@app.websocket("/ws/esp")
async def websocket_esp(websocket: WebSocket):
    # FIX: Accept the connection *before* trying to read from it.
    await websocket.accept()
    
    machine_id = None
    try:
        # Now that the connection is accepted, we can safely wait for the first message.
        initial_data = json.loads(await websocket.receive_text())
        machine_id = initial_data.get("machine_id") or initial_data.get("id")
        
        if not machine_id:
            print("Closing connection: ESP32 did not send a machine_id on connect.")
            # We don't need to call close() here, the finally block will handle it.
            return

        # Use the modified .connect() method which no longer calls .accept()
        await manager.connect(websocket, machine_id)
        print(f"ESP '{machine_id}' connected.")
        
        # The first message also needs to be processed
        process_esp_message(initial_data, machine_id)

        # Main loop for listening to all subsequent messages
        while True:
            data = json.loads(await websocket.receive_text())
            process_esp_message(data, machine_id)

    except WebSocketDisconnect:
        print(f"ESP '{machine_id}' disconnected.")
    finally:
        # This will now correctly handle cleanup
        if machine_id:
            manager.disconnect(machine_id)

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

# --- REPLACE THE EXISTING /command FUNCTION WITH THIS ---
@app.post("/command")
async def send_command(request: Request):
    data = await request.json()
    machine_id, command_str = data.get("id"), data.get("command")

    if not machine_id or not command_str:
        return {"status": "error", "message": "Missing id or command"}
    
    # Use the manager to send commands immediately
    if command_str == "stop":
        await manager.send_command(machine_id, {"action": "toggle_monitoring"})

    elif command_str == "restart":
        # Reset server state first
        if machine_id in machines:
            machines[machine_id].update({"baseline_state": "needed", "status": "idle"})
            # Clear all data buffers
            for key in ["baseline_x_buffer", "baseline_y_buffer", "baseline_z_buffer", "x_buffer", "y_buffer", "z_buffer", "x_smooth", "y_smooth", "z_smooth"]:
                machines[machine_id][key] = []
        
        # Then send commands to ESP32
        await manager.send_command(machine_id, {"action": "reset_state"})
        await manager.send_command(machine_id, {"action": "set_leds", "red": "off", "yellow": "off"})
    
    return {"status": "ok", "message": f"Command '{command_str}' sent."}


# In main.py, find and completely replace your old get_history function with this one.
# Make sure you have "import pandas as pd" at the top of your file.

# In main.py, replace the entire get_history function with this block.

# In main.py, replace the entire get_history function.

@app.get("/api/history/{machine_id}")
async def get_history(machine_id: str):
    if not query_api:
        return JSONResponse(status_code=503, content={"error": "InfluxDB query service not available"})

    # --- (CORRECTED v3) Flux Query for DHT ---
    # We REMOVED the strict filter to ensure all data is returned.
    flux_query_dht = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
        |> range(start: -30d)
        |> filter(fn: (r) => r["_measurement"] == "machine_data")
        |> filter(fn: (r) => r["machine_id"] == "{machine_id}")
        |> filter(fn: (r) => r["_field"] == "temperature" or r["_field"] == "humidity")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> sort(columns: ["_time"], desc: true)
        |> limit(n: 300)
        |> group()
        |> sort(columns: ["_time"], desc: false)
    '''

    # --- (CORRECTED v3) Flux Query for Accelerometer ---
    # We REMOVED the strict filter here as well.
    flux_query_accel = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
        |> range(start: -30d)
        |> filter(fn: (r) => r["_measurement"] == "machine_data")
        |> filter(fn: (r) => r["machine_id"] == "{machine_id}")
        |> filter(fn: (r) => r["_field"] == "x_mean" or r["_field"] == "y_mean" or r["_field"] == "z_mean")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> sort(columns: ["_time"], desc: true)
        |> limit(n: 300)
        |> group()
        |> sort(columns: ["_time"], desc: false)
    '''

    try:
        print(f"Querying 300-point history for {machine_id}...")

        result_dht = query_api.query_data_frame(query=flux_query_dht)
        result_accel = query_api.query_data_frame(query=flux_query_accel)

        def format_for_charts(df):
            if df.empty:
                return {}
            df_formatted = df.copy() # Use .copy() to be safe
            df_formatted['_time'] = df_formatted['_time'].astype(str)
            return df_formatted.to_dict(orient='list')

        dht_data = format_for_charts(result_dht)
        accel_data = format_for_charts(result_accel)

        return {"dht": dht_data, "accelerometer": accel_data}

    except Exception as e:
        print(f"Error querying InfluxDB: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to query or process historical data"})