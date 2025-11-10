// --- Global object to hold our chart instances ---
const historyCharts = {
    temperature: null,
    humidity: null,
    accelerometer: null
};

// This helper function creates the wrapper and canvas for a chart
function createChartCanvas(id, container) {
    const wrapper = document.createElement('div');
    wrapper.className = 'chart-wrapper';
    
    const canvas = document.createElement('canvas');
    canvas.id = id;
    wrapper.appendChild(canvas);

    container.appendChild(wrapper);
    return canvas.getContext('2d');
}

// --- The definitive function to load or update historical data ---
async function loadHistoricalData(machineId, isInitialLoad = false) {
    const gridContainer = document.getElementById('history-grid');
    if (!gridContainer) return;

    if (isInitialLoad) {
        gridContainer.innerHTML = `<p style="color:#9fbce8;">Loading initial history for ${machineId}...</p>`;
    }

    try {
        const response = await fetch(`/api/history/${machineId}`);
        if (!response.ok) {
            if (isInitialLoad) gridContainer.innerHTML = `<p style="color:red;">Failed to fetch history.</p>`;
            return;
        }
        const data = await response.json();

        if (isInitialLoad) {
            gridContainer.innerHTML = '';
        }

        const chartOptions = (yAxisTitle) => ({
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 400 },
            scales: {
                x: { type: 'time', time: { unit: 'day', tooltipFormat: 'MMM dd, HH:mm' }, ticks: { color: '#9fbce8' }, grid: { color: 'rgba(255,255,255,0.1)' } },
                y: { title: { display: true, text: yAxisTitle, color: '#cfe7ff' }, ticks: { color: '#9fbce8', beginAtZero: false }, grid: { color: 'rgba(255,255,255,0.1)' } }
            },
            plugins: { legend: { labels: { color: '#e6f0ff' } } }
        });

        // --- CORRECTED LOGIC for Temperature & Humidity ---
        // We now handle both charts inside a single, more robust check.
        if (data.dht && Object.keys(data.dht).length > 0) {
            // Use '|| []' to provide an empty array if a key is missing, preventing errors.
            const tempData = { labels: data.dht._time || [], datasets: [{ label: 'Temperature (°C)', data: data.dht.temperature || [], borderColor: 'rgb(255, 99, 132)', tension: 0.2, pointRadius: 1 }] };
            const humData = { labels: data.dht._time || [], datasets: [{ label: 'Humidity (%)', data: data.dht.humidity || [], borderColor: 'rgb(54, 162, 235)', tension: 0.2, pointRadius: 1 }] };
            
            // Update or create Temperature chart
            if (historyCharts.temperature) {
                historyCharts.temperature.data = tempData;
                historyCharts.temperature.update();
            } else {
                const ctx = createChartCanvas('temp-history', gridContainer);
                historyCharts.temperature = new Chart(ctx, { type: 'line', data: tempData, options: chartOptions('Temperature (°C)') });
            }

            // Update or create Humidity chart
            if (historyCharts.humidity) {
                historyCharts.humidity.data = humData;
                historyCharts.humidity.update();
            } else {
                const ctx = createChartCanvas('hum-history', gridContainer);
                historyCharts.humidity = new Chart(ctx, { type: 'line', data: humData, options: chartOptions('Humidity (%)') });
            }
        }
        
        // --- CORRECTED LOGIC for Accelerometer ---
        if (data.accelerometer && Object.keys(data.accelerometer).length > 0) {
            const accelData = {
                labels: data.accelerometer._time || [],
                datasets: [
                    { label: 'X-Axis', data: data.accelerometer.x_mean || [], borderColor: 'rgb(255, 206, 86)', tension: 0.2, pointRadius: 1 },
                    { label: 'Y-Axis', data: data.accelerometer.y_mean || [], borderColor: 'rgb(75, 192, 192)', tension: 0.2, pointRadius: 1 },
                    { label: 'Z-Axis', data: data.accelerometer.z_mean || [], borderColor: 'rgb(153, 102, 255)', tension: 0.2, pointRadius: 1 }
                ]
            };
            if (historyCharts.accelerometer) {
                historyCharts.accelerometer.data = accelData;
                historyCharts.accelerometer.update();
            } else {
                const ctx = createChartCanvas('accel-history', gridContainer);
                historyCharts.accelerometer = new Chart(ctx, { type: 'line', data: accelData, options: chartOptions('Accel. Mean') });
            }
        }

    } catch (error) {
        console.error("Error loading historical data:", error);
    }
}