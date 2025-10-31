#include <WiFi.h>
#include <ArduinoJson.h>
#include <ArduinoWebsockets.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>

using namespace websockets;

// --- Pin Definitions ---
#define CONFIG_BUTTON 12
#define STOP_BUTTON 14
#define LED_PIN 2
#define YELLOW_LED_PIN 26
#define RED_LED_PIN    27

// --- Network Credentials ---
const char* ssid = "The Rider Aadi ";
const char* password = "papajiii";
const char* websocketServer = "ws://10.114.69.140:8000/ws/esp";

// --- State Variables ---
bool monitoringActive = false;
bool yellowLedBlinking = false;

// ### NEW: Baseline State Machine ###
enum BaselineState { NOT_STARTED, COLLECTING, SENDING, COMPLETE };
BaselineState currentBaselineState = NOT_STARTED;

// --- Non-Blocking Timer Variables ---
unsigned long previousBlinkMillis = 0;
const long blinkInterval = 500;
unsigned long lastSampleTime = 0;
unsigned long lastStatusBlinkTime = 0;
unsigned long buttonPressTime = 0;
unsigned long lastSendTime = 0; // For chunk sending

// --- Sampling Buffers ---
#define BASELINE_SAMPLES 3000 // Increased for a robust baseline
#define CHUNK_SIZE 100
#define WINDOW_SIZE 100

// ### MODIFIED: Buffers are now global for the state machine ###
float xBuffer[WINDOW_SIZE], yBuffer[WINDOW_SIZE], zBuffer[WINDOW_SIZE];
float baselineX[BASELINE_SAMPLES], baselineY[BASELINE_SAMPLES], baselineZ[BASELINE_SAMPLES];
int bufferIndex = 0;
int baselineSampleIndex = 0;
int baselineChunkIndex = 0;

// --- Other Globals ---
WebsocketsClient client;
Adafruit_ADXL345_Unified accel = Adafruit_ADXL345_Unified(12345);

// --- Function Declarations ---
void readAccelerometer(float &x, float &y, float &z);
void sendJSON(const char* type, int samples, float* x, float* y, float* z);

// --- WebSocket Message Handler ---
void onMessage(WebsocketsMessage msg) {
  Serial.print("Received command: "); Serial.println(msg.data());
  DynamicJsonDocument doc(512);

  DeserializationError err = deserializeJson(doc, msg.data());
  if (err) {
    Serial.print("JSON parse error: ");
    Serial.println(err.c_str());
    return;
  }

  JsonObject command = doc["command"];
  if (!command.isNull()) {
    const char* action = command["action"];
    if (action && strcmp(action, "set_leds") == 0) {
      const char* redState = command["red"];
      const char* yellowState = command["yellow"];

      digitalWrite(RED_LED_PIN, (strcmp(redState, "on") == 0) ? HIGH : LOW);

      if (strcmp(yellowState, "blink") == 0) {
        yellowLedBlinking = true;
      } else {
        yellowLedBlinking = false;
        digitalWrite(YELLOW_LED_PIN, (strcmp(yellowState, "on") == 0) ? HIGH : LOW);
      }
    }
  }
}

// --- Setup Function ---
void setup() {
  Serial.begin(115200);
  pinMode(CONFIG_BUTTON, INPUT_PULLUP);
  pinMode(STOP_BUTTON, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(YELLOW_LED_PIN, OUTPUT);
  pinMode(RED_LED_PIN, OUTPUT);

  if(!accel.begin()) { Serial.println("ADXL345 Error"); while(1); }
  accel.setRange(ADXL345_RANGE_16_G);

  WiFi.begin(ssid, password);
  Serial.print("Connecting to Wi-Fi...");
  while (WiFi.status() != WL_CONNECTED) { Serial.print("."); delay(500); }
  Serial.println("\nWi-Fi connected!");

  client.onMessage(onMessage);
  while(!client.connect(websocketServer)) { Serial.println("Connection failed, retrying..."); delay(2000); }
  Serial.println("WebSocket connected!");
}

// --- Main Loop ---
void loop() {
  unsigned long currentMillis = millis();
  client.poll(); // ### CRITICAL: This MUST be called frequently to keep the connection alive!

  // --- Blinker Logic ---
  if (yellowLedBlinking) {
    if (currentMillis - previousBlinkMillis >= blinkInterval) {
      previousBlinkMillis = currentMillis;
      digitalWrite(YELLOW_LED_PIN, !digitalRead(YELLOW_LED_PIN));
    }
  }

  // --- Button Logic (Non-Blocking with Debounce) ---
  if (currentMillis - buttonPressTime > 250) {
    // --- CONFIG BUTTON PRESS ---
    if (digitalRead(CONFIG_BUTTON) == LOW && currentBaselineState == NOT_STARTED) {
      buttonPressTime = currentMillis;
      currentBaselineState = COLLECTING;
      baselineSampleIndex = 0; // Reset index
      digitalWrite(LED_PIN, HIGH); // Solid light indicates collection
      Serial.println("Starting non-blocking baseline collection...");
    }
    // --- STOP BUTTON PRESS ---
    if (digitalRead(STOP_BUTTON) == LOW && currentBaselineState == COMPLETE) {
      buttonPressTime = currentMillis;
      monitoringActive = !monitoringActive;
      Serial.print("Monitoring Active: "); Serial.println(monitoringActive);
    }
  }

  // ### NEW: Non-Blocking Baseline State Machine ###
  // --- STATE: COLLECTING SAMPLES ---
  if (currentBaselineState == COLLECTING) {
    if (currentMillis - lastSampleTime >= 10) { // Sample at ~100 Hz
      lastSampleTime = currentMillis;
      if (baselineSampleIndex < BASELINE_SAMPLES) {
        readAccelerometer(baselineX[baselineSampleIndex], baselineY[baselineSampleIndex], baselineZ[baselineSampleIndex]);
        baselineSampleIndex++;
      } else {
        // We have all samples, move to next state
        currentBaselineState = SENDING;
        baselineChunkIndex = 0; // Reset chunk index for sending
        Serial.println("Sample collection complete. Sending to server...");
      }
    }
  }

  // --- STATE: SENDING SAMPLES IN CHUNKS ---
  if (currentBaselineState == SENDING) {
    if (currentMillis - lastSendTime >= 50) { // Send a chunk every 50ms
      lastSendTime = currentMillis;
      if (baselineChunkIndex < BASELINE_SAMPLES) {
        int chunk = (baselineChunkIndex + CHUNK_SIZE <= BASELINE_SAMPLES) ? CHUNK_SIZE : (BASELINE_SAMPLES - baselineChunkIndex);
        sendJSON("configure", chunk, &baselineX[baselineChunkIndex], &baselineY[baselineChunkIndex], &baselineZ[baselineChunkIndex]);
        baselineChunkIndex += CHUNK_SIZE;
      } else {
        // We have sent all chunks, finalize
        currentBaselineState = COMPLETE;
        monitoringActive = true;
        digitalWrite(LED_PIN, LOW); // Turn off status light
        Serial.println("Baseline sent, monitoring active.");
      }
    }
  }

  // --- Status LED (before config) ---
  if (currentBaselineState == NOT_STARTED) {
    if (currentMillis - lastStatusBlinkTime >= 300) {
      lastStatusBlinkTime = currentMillis;
      digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
  }

  // --- Sensor Monitoring (only runs when baseline is complete) ---
  if (monitoringActive && currentBaselineState == COMPLETE) {
    if (currentMillis - lastSampleTime >= 10) { // ~100 Hz
      lastSampleTime = currentMillis;
      readAccelerometer(xBuffer[bufferIndex], yBuffer[bufferIndex], zBuffer[bufferIndex]);
      bufferIndex++;

      if (bufferIndex >= WINDOW_SIZE) {
        sendJSON("data", bufferIndex, xBuffer, yBuffer, zBuffer);
        bufferIndex = 0;
      }
    }
  }
}

// --- Function Definitions (No changes here) ---
void readAccelerometer(float &x, float &y, float &z) {
  sensors_event_t event;
  accel.getEvent(&event);
  x = event.acceleration.x;
  y = event.acceleration.y;
  z = event.acceleration.z;
}

void sendJSON(const char* type, int samples, float* x, float* y, float* z) {
  // Increased size for safety with larger chunks, though 8192 should be fine.
  DynamicJsonDocument doc(10000); 
  doc["machine_id"] = "MACHINE_1";
  doc["type"] = type;
  JsonArray xArray = doc.createNestedArray("x");
  JsonArray yArray = doc.createNestedArray("y");
  JsonArray zArray = doc.createNestedArray("z");

  for(int i=0; i<samples; i++){
    xArray.add(x[i]);
    yArray.add(y[i]);
    zArray.add(z[i]);
  }

  String payload;
  serializeJson(doc, payload);
  client.send(payload);
}