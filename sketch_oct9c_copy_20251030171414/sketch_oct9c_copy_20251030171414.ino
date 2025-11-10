#include <WiFi.h>
#include <ArduinoJson.h>
#include <ArduinoWebsockets.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>
#include <DHT.h>  // ✅ NEW

using namespace websockets;

// --- Pin Definitions ---
#define CONFIG_BUTTON 12
#define STOP_BUTTON 14
#define LED_PIN 2
#define YELLOW_LED_PIN 26
#define RED_LED_PIN 27
#define DHTPIN 4           // ✅ DHT11 Data pin connected to GPIO 4
#define DHTTYPE DHT11      // ✅ Sensor type

// --- Network Credentials ---
const char* ssid = "KnightE4";
const char* password = "knightE0";
const char* websocketServer = "ws://10.210.160.140:8000/ws/esp";

// --- State Variables ---
bool monitoringActive = false;
bool yellowLedBlinking = false;

// --- Baseline State Machine ---
enum BaselineState { NOT_STARTED, COLLECTING, SENDING, COMPLETE };
BaselineState currentBaselineState = NOT_STARTED;

// --- Timers ---
unsigned long previousBlinkMillis = 0;
const long blinkInterval = 500;
unsigned long lastSampleTime = 0;
unsigned long lastStatusBlinkTime = 0;
unsigned long buttonPressTime = 0;
unsigned long lastSendTime = 0;
unsigned long lastKeepAliveTime = 0;
unsigned long lastDhtSendTime = 0;

// --- Buffers ---
#define BASELINE_SAMPLES 3000
#define CHUNK_SIZE 100
#define WINDOW_SIZE 100
#define JSON_BUFFER_SIZE 10240

float xBuffer[WINDOW_SIZE], yBuffer[WINDOW_SIZE], zBuffer[WINDOW_SIZE];
float baselineX[BASELINE_SAMPLES], baselineY[BASELINE_SAMPLES], baselineZ[BASELINE_SAMPLES];
int bufferIndex = 0;
int baselineSampleIndex = 0;
int baselineChunkIndex = 0;

// ✅ FIX: Declare the global character buffer for JSON serialization
char json_buffer[JSON_BUFFER_SIZE];


// --- DHT Sensor ---
DHT dht(DHTPIN, DHTTYPE); // ✅ DHT object
float baselineTempSum = 0, baselineHumSum = 0; // ✅ Accumulators
int dhtSampleCount = 0;
float baselineTempAvg = 0, baselineHumAvg = 0;

// --- Other Globals ---
WebsocketsClient client;
Adafruit_ADXL345_Unified accel = Adafruit_ADXL345_Unified(12345);
// --- ADD THIS near the top with other globals ---
volatile bool is_connected = false;

// --- Function Declarations ---
void readAccelerometer(float &x, float &y, float &z);
void sendJSON(const char* type, int samples, float* x, float* y, float* z);
void sendDHTData(const char* type, float temperature, float humidity); // ✅ NEW
void onEvents(WebsocketsEvent event, String data); // Forward declaration


// --- ADD THIS NEW FUNCTION IN your sketch.ino file ---

// Sends a one-time message to identify the device to the server on connect.
void sendIdentification() {
  DynamicJsonDocument doc(256);
  doc["machine_id"] = "MACHINE_1";
  doc["type"] = "identify"; // A new, unique message type
  
  size_t len = serializeJson(doc, json_buffer, JSON_BUFFER_SIZE);
  client.send(json_buffer, len);
  Serial.println("Sent identification packet to server.");
}

// --- CORRECTED onMessage FUNCTION ---
void onMessage(WebsocketsMessage msg) {
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, msg.data())) return;

  JsonObject command = doc["command"];
  if (!command.isNull()) {
    const char* action = command["action"];

    if (action && strcmp(action, "set_leds") == 0) {
      const char* redState = command["red"];
      const char* yellowState = command["yellow"];
      digitalWrite(RED_LED_PIN, (strcmp(redState, "on") == 0) ? HIGH : LOW);
      
      // ✅ FIX: Changed yellowBlinking back to the correct variable name yellowLedBlinking
      if (strcmp(yellowState, "blink") == 0) { 
        yellowLedBlinking = true; 
      } else { 
        yellowLedBlinking = false; 
        digitalWrite(YELLOW_LED_PIN, (strcmp(yellowState, "on") == 0) ? HIGH : LOW); 
      }
    } 
    else if (action && strcmp(action, "reset_state") == 0) {
      Serial.println("Received 'reset_state' command. Returning to idle.");
      monitoringActive = false;
      currentBaselineState = NOT_STARTED;
      // ✅ FIX: Changed yellowBlinking back to the correct variable name yellowLedBlinking
      yellowLedBlinking = false;
    } 
    else if (action && strcmp(action, "toggle_monitoring") == 0) {
      if (currentBaselineState == COMPLETE) {
        monitoringActive = !monitoringActive;
        Serial.print("Received 'toggle_monitoring'. Monitoring Active: ");
        Serial.println(monitoringActive);
      }
    }
  }
}

// --- ADD THIS new function anywhere in your .ino file ---
void onEvents(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionOpened) {
        Serial.println("Connnection Opened");
        is_connected = true;
    } else if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("Connnection Closed");
        is_connected = false;
    }
}

// --- Setup ---
void setup() {
  Serial.begin(115200);
  pinMode(CONFIG_BUTTON, INPUT_PULLUP);
  pinMode(STOP_BUTTON, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(YELLOW_LED_PIN, OUTPUT);
  pinMode(RED_LED_PIN, OUTPUT);

  if (!accel.begin()) { Serial.println("ADXL345 Error"); while (1); }
  accel.setRange(ADXL345_RANGE_16_G);
  dht.begin(); // ✅ Initialize DHT

  WiFi.begin(ssid, password);
  Serial.print("Connecting to Wi-Fi...");
  while (WiFi.status() != WL_CONNECTED) { Serial.print("."); delay(500); }
  Serial.println("\nWi-Fi connected!");

  client.onMessage(onMessage);
  client.onEvent(onEvents);
  while (!client.connect(websocketServer)) { Serial.println("WS connect fail, retry..."); delay(2000); }
  Serial.println("WebSocket connected!");

  // ESP32 telling its Identity to the server...
  sendIdentification();
}

// --- Loop ---
// --- REPLACE your existing loop() function with this ---

void loop() {

  // First, check if the client is connected.
  if (is_connected) {
    
    // ---------------------------------------------------
    // --- NORMAL OPERATION: All your original code goes here ---
    // ---------------------------------------------------
    
    client.poll(); // Poll for messages from the server.
    
    unsigned long currentMillis = millis();

    // --- LED Blinking ---
    if (yellowLedBlinking && currentMillis - previousBlinkMillis >= blinkInterval) {
      previousBlinkMillis = currentMillis;
      digitalWrite(YELLOW_LED_PIN, !digitalRead(YELLOW_LED_PIN));
    }

    // --- Button Handling ---
    if (currentMillis - buttonPressTime > 250) {
      if (digitalRead(CONFIG_BUTTON) == LOW && currentBaselineState == NOT_STARTED) {
        buttonPressTime = currentMillis;
        currentBaselineState = COLLECTING;
        baselineSampleIndex = 0;
        baselineTempSum = baselineHumSum = 0;
        dhtSampleCount = 0;
        digitalWrite(LED_PIN, HIGH);
        Serial.println("Starting baseline collection...");
      }

      if (digitalRead(STOP_BUTTON) == LOW && currentBaselineState == COMPLETE) {
        buttonPressTime = currentMillis;
        monitoringActive = !monitoringActive;
        Serial.print("Monitoring Active: "); Serial.println(monitoringActive);
      }
    }

    // --- Baseline Collection ---
    if (currentBaselineState == COLLECTING) {
      if (currentMillis - lastKeepAliveTime >= 20000) {
        lastKeepAliveTime = currentMillis;
        strcpy(json_buffer, "{\"type\":\"ping\"}"); 
        client.send(json_buffer);
        Serial.println("-> Sent application-level keep-alive ping.");
      }
      if (currentMillis - lastSampleTime >= 10) {
        lastSampleTime = currentMillis;
        if (baselineSampleIndex < BASELINE_SAMPLES) {
          readAccelerometer(baselineX[baselineSampleIndex], baselineY[baselineSampleIndex], baselineZ[baselineSampleIndex]);
          baselineSampleIndex++;
        }
        if (baselineSampleIndex % 100 == 0) {
          float t = dht.readTemperature();
          float h = dht.readHumidity();
          if (!isnan(t) && !isnan(h)) {
            baselineTempSum += t;
            baselineHumSum += h;
            dhtSampleCount++;
          }
        }
        if (baselineSampleIndex >= BASELINE_SAMPLES) {
          currentBaselineState = SENDING;
          baselineChunkIndex = 0;
          if (dhtSampleCount > 0) {
            baselineTempAvg = baselineTempSum / dhtSampleCount;
            baselineHumAvg = baselineHumSum / dhtSampleCount;
          }
          Serial.println("Baseline collection complete. Sending to server...");
        }
      }
    }

    // --- Baseline Sending ---
    if (currentBaselineState == SENDING) {
      if (currentMillis - lastSendTime >= 50) {
        lastSendTime = currentMillis;
        if (baselineChunkIndex < BASELINE_SAMPLES) {
          int chunk = (baselineChunkIndex + CHUNK_SIZE <= BASELINE_SAMPLES)
            ? CHUNK_SIZE : (BASELINE_SAMPLES - baselineChunkIndex);
          sendJSON("configure", chunk, &baselineX[baselineChunkIndex],
                   &baselineY[baselineChunkIndex], &baselineZ[baselineChunkIndex]);
          baselineChunkIndex += CHUNK_SIZE;
        } else {
          sendDHTData("baseline_dht", baselineTempAvg, baselineHumAvg);
          currentBaselineState = COMPLETE;
          monitoringActive = true;
          digitalWrite(LED_PIN, LOW);
          Serial.println("Baseline sent, monitoring active.");
        }
      }
    }

    // --- Paused State Ping ---
    if (currentBaselineState == COMPLETE && !monitoringActive) {
      if (currentMillis - lastKeepAliveTime >= 20000) {
        lastKeepAliveTime = currentMillis;
        strcpy(json_buffer, "{\"type\":\"ping\"}"); 
        client.send(json_buffer);
        Serial.println("-> Sent keep-alive ping while paused.");
      }
    }

    // --- Status LED for Idle State ---
    if (currentBaselineState == NOT_STARTED && currentMillis - lastStatusBlinkTime >= 300) {
      lastStatusBlinkTime = currentMillis;
      digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }

    // --- Monitoring Mode ---
    if (monitoringActive && currentBaselineState == COMPLETE) {
      if (currentMillis - lastSampleTime >= 10) {
        lastSampleTime = currentMillis;
        readAccelerometer(xBuffer[bufferIndex], yBuffer[bufferIndex], zBuffer[bufferIndex]);
        bufferIndex++;
        if (bufferIndex >= WINDOW_SIZE) {
          sendJSON("data", bufferIndex, xBuffer, yBuffer, zBuffer);
          bufferIndex = 0;
        }
      }
      if (currentMillis - lastDhtSendTime >= 2000) {
          lastDhtSendTime = currentMillis;
          float t = dht.readTemperature();
          float h = dht.readHumidity();
          if (!isnan(t) && !isnan(h)) {
            sendDHTData("dht", t, h);
          }
      }
    }
    
  } else {
    
    // ---------------------------------------------------------------
    // --- DISCONNECTED STATE: Halt all operations until reset ---
    // ---------------------------------------------------------------
    
    // This condition ensures the "Halt" message is printed only ONCE
    // when the device transitions from an active state to a disconnected one.
    if (currentBaselineState != NOT_STARTED) {
      Serial.println("\n-------------------------------------------");
      Serial.println("FATAL: Connection to server lost.");
      Serial.println("Halting all operations. Please reset device.");
      Serial.println("-------------------------------------------");

      // Set variables to a safe, stopped state.
      monitoringActive = false;
      currentBaselineState = NOT_STARTED;
      yellowLedBlinking = false;
      
      // Set LEDs to a clear "error/halted" state (e.g., solid red).
      digitalWrite(RED_LED_PIN, LOW);
      digitalWrite(YELLOW_LED_PIN, LOW);
      digitalWrite(LED_PIN, LOW);
    }
    
    // Do nothing but wait. The device is now effectively halted from an
    // operational standpoint and needs to be manually reset.
    delay(1000);
  }
}


// --- Functions ---
void readAccelerometer(float &x, float &y, float &z) {
  sensors_event_t event;
  accel.getEvent(&event);
  x = event.acceleration.x;
  y = event.acceleration.y;
  z = event.acceleration.z;
}

// ✅ FIX: Use the global buffer to prevent memory fragmentation
void sendJSON(const char* type, int samples, float* x, float* y, float* z) {
  DynamicJsonDocument doc(10000);
  doc["machine_id"] = "MACHINE_1";
  doc["type"] = type;
  JsonArray xa = doc.createNestedArray("x");
  JsonArray ya = doc.createNestedArray("y");
  JsonArray za = doc.createNestedArray("z");
  for (int i = 0; i < samples; i++) {
    xa.add(x[i]); ya.add(y[i]); za.add(z[i]);
  }
  size_t len = serializeJson(doc, json_buffer, JSON_BUFFER_SIZE);
  client.send(json_buffer, len);
}

// ✅ FIX: Use the global buffer to prevent memory fragmentation
void sendDHTData(const char* type, float temperature, float humidity) {
  DynamicJsonDocument doc(512);
  doc["machine_id"] = "MACHINE_1";
  doc["type"] = type;
  doc["temperature"] = temperature;
  doc["humidity"] = humidity;
  size_t len = serializeJson(doc, json_buffer, JSON_BUFFER_SIZE);
  client.send(json_buffer, len);
  Serial.printf("[%s] Temp=%.2f°C  Hum=%.2f%%\n", type, temperature, humidity);
}