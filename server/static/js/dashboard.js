const ws = new WebSocket(`ws://${window.location.host}/ws/dashboard`);
const machinesContainer = document.getElementById("machines-container");
const machineCharts = {};
const MAX_PLOT = 200;

ws.onopen = () => console.log("Dashboard WS open");
ws.onmessage = (ev) => {
  try {
    const data = JSON.parse(ev.data);
    for (let id in data) {
      if (!machineCharts[id]) addMachinePanel(id);
      updateMachinePanel(id, data[id]);
    }
  } catch (e) {
    console.error("Failed to parse dashboard message", e);
  }
};
ws.onclose = () => console.log("Dashboard WS closed");

function addMachinePanel(id) {
  const container = document.createElement("div");
  container.className = "machine-panel";
  container.id = `machine-${id}`;
  container.style.border = "1px solid rgba(255,255,255,0.06)";
  container.style.padding = "10px";
  container.style.marginBottom = "12px";
  container.style.borderRadius = "12px";
  container.style.background = "rgba(10,20,30,0.6)";

  container.innerHTML = `
    <div class="panel-head" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="status-indicator" id="status-${id}" style="width:12px;height:12px;border-radius:50%;display:inline-block;transition:transform .25s, box-shadow .25s;"></span>
        <strong style="color:#e6f0ff;">${id}</strong>
      </div>
      <div>
        <button onclick="sendCommand('${id}', 'stop')" style="margin-right:6px;">Stop</button>
        <button onclick="sendCommand('${id}', 'restart')">Restart</button>
      </div>
    </div>
    <div style="display:flex;gap:8px;">
      <div style="flex:1;height:170px;"><canvas id="vib-${id}"></canvas></div>
      <div style="width:420px;height:170px;"><canvas id="fft-${id}"></canvas></div>
    </div>
    <div style="color:#9fbce8;margin-top:6px;font-size:12px;">Showing latest ${MAX_PLOT} smoothed samples · FFT: frequency (Hz)</div>
  `;
  machinesContainer.appendChild(container);

  const ctxV = container.querySelector(`#vib-${id}`).getContext('2d');
  const ctxF = container.querySelector(`#fft-${id}`).getContext('2d');

  machineCharts[id] = {
    vibration: new Chart(ctxV, {
      type: 'line',
      data: {
        labels: Array(MAX_PLOT).fill(''),
        datasets: [
          { label: 'X', data: [], borderColor: '#fb7185', pointRadius: 0, tension: 0.35, borderWidth: 2 },
          { label: 'Y', data: [], borderColor: '#34d399', pointRadius: 0, tension: 0.35, borderWidth: 2 },
          { label: 'Z', data: [], borderColor: '#60a5fa', pointRadius: 0, tension: 0.35, borderWidth: 2 }
        ]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#cfe7ff' } }, decimation: { enabled: true, algorithm: 'lttb', samples: 100 } },
        scales: { x: { display: false }, y: { ticks: { color: '#9fbce8' } } }
      }
    }),
    fft: new Chart(ctxF, {
      type: 'line',
      data: {
        datasets: [
          { label: 'FFT X (dB)', data: [], borderColor: '#fb7185', pointRadius: 0, tension: 0.2, borderWidth: 1 },
          { label: 'FFT Y (dB)', data: [], borderColor: '#34d399', pointRadius: 0, tension: 0.2, borderWidth: 1 },
          { label: 'FFT Z (dB)', data: [], borderColor: '#60a5fa', pointRadius: 0, tension: 0.2, borderWidth: 1 }
        ]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#cfe7ff' } } },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Frequency (Hz)', color: '#cfe7ff' },
            ticks: { color: '#9fbce8' },
            grid: { color: 'rgba(255,255,255,0.03)' }
          },
          y: {
            title: { display: true, text: 'Magnitude (dB)', color: '#cfe7ff' },
            ticks: { color: '#9fbce8' },
            grid: { color: 'rgba(255,255,255,0.03)' }
          }
        }
      }
    })
  };
}

function padTo(arr, n) {
  if (!arr) return Array(n).fill(null);
  const a = arr.slice(-n);
  if (a.length < n) {
    return Array(n - a.length).fill(null).concat(a);
  }
  return a;
}

function updateMachinePanel(id, machine) {
  const statusEl = document.getElementById(`status-${id}`);
  const color = machine.status === 'active' ? '#10b981' : machine.status === 'warning' ? '#f59e0b' : '#ef4444';
  if (statusEl) {
    statusEl.style.backgroundColor = color;
    statusEl.style.transform = 'scale(1.3)';
    statusEl.style.boxShadow = `0 0 8px ${color}66`;
    setTimeout(() => {
      statusEl.style.transform = 'scale(1.0)';
      statusEl.style.boxShadow = 'none';
    }, 220);
  }

  const charts = machineCharts[id];
  if (!charts) return;

  const xs = (machine.x || []).slice(-MAX_PLOT);
  const ys = (machine.y || []).slice(-MAX_PLOT);
  const zs = (machine.z || []).slice(-MAX_PLOT);

  charts.vibration.data.datasets[0].data = padTo(xs, MAX_PLOT);
  charts.vibration.data.datasets[1].data = padTo(ys, MAX_PLOT);
  charts.vibration.data.datasets[2].data = padTo(zs, MAX_PLOT);
  charts.vibration.update('none');

  const freqs = machine.fft_freqs || [];
  const fx = machine.fft_x_db || [];
  const fy = machine.fft_y_db || [];
  const fz = machine.fft_z_db || [];

  if (freqs.length && (fx.length || fy.length || fz.length)) {
    // Chart.js needs {x:freq, y:db} objects for linear x-axis
    const pointsX = freqs.slice(0, fx.length).map((f, i) => ({ x: f, y: fx[i] }));
    const pointsY = freqs.slice(0, fy.length).map((f, i) => ({ x: f, y: fy[i] }));
    const pointsZ = freqs.slice(0, fz.length).map((f, i) => ({ x: f, y: fz[i] }));

    charts.fft.data.datasets[0].data = pointsX;
    charts.fft.data.datasets[1].data = pointsY;
    charts.fft.data.datasets[2].data = pointsZ;
    charts.fft.update('none');
  } else {
    // fallback, clear fft
    charts.fft.data.datasets[0].data = [];
    charts.fft.data.datasets[1].data = [];
    charts.fft.data.datasets[2].data = [];
    charts.fft.update('none');
  }
}

function sendCommand(id, command) {
  fetch('/command', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ id, command })});
}
