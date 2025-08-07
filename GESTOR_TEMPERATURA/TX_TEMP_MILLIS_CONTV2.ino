#include <WiFi.h>
#include <PubSubClient.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// --- Pines ---
#define ONE_WIRE_BUS 2   // GPIO2 - sensor DS18B20
#define RELAY_PIN 4      // GPIO4 - relé
#define SERVO_PIN 18     // GPIO18 - señal servo

// --- Config PWM servo ---
#define SERVO_CH   0     // Canal PWM
#define SERVO_FREQ 50    // 50 Hz (20 ms)
#define SERVO_RES  16    // 16 bits resolución

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);

const char* ssid     = "s6";
const char* password = "claveesp32";

const char* mqtt_server = "192.168.10.169";
const int mqtt_port = 1883;
const char* topic = "temp/sensor";
const char* topic_millis = "meds/millis";
const char* topic_ref = "temp/ref";

WiFiClient espClient;
PubSubClient client(espClient);

float referencia = 100.0;
unsigned long lastTempRead = 0;
const unsigned long tempInterval = 1000; // 1 segundo

// --- Función para mover servo sin librería ---
void moverServo(int angulo) {
  int pulsoMin = 500;   // µs para 0°
  int pulsoMax = 2400;  // µs para 180°
  int pulso = map(angulo, 0, 180, pulsoMin, pulsoMax);
  int duty = (pulso * 65535) / 20000; // conversión a duty 16 bits
  ledcWrite(SERVO_CH, duty);
}

void callback(char* topic, byte* message, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) {
    msg += (char)message[i];
  }
  Serial.printf("📩 Mensaje en [%s]: %s\n", topic, msg.c_str());

  if (String(topic) == "temp/ref") {
    referencia = msg.toFloat();
    Serial.printf("🌡️ Referencia ACTUALIZADA a: %.2f\n", referencia);
  }
}

void setup_wifi() {
  WiFi.begin(ssid, password);
  Serial.printf("Conectando a %s\n", ssid);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\n✅ WiFi conectado - IP: %s\n", WiFi.localIP().toString().c_str());
}

void reconnect() {
  while (!client.connected()) {
    Serial.print("Intentando conexión MQTT...");
    if (client.connect("ESP32Client")) {
      Serial.println("✅ Conectado");
      client.subscribe(topic_ref);
      Serial.println("📡 Suscrito a temp/ref");
    } else {
      Serial.printf("❌ Falló, rc=%d. Reintentando en 5s\n", client.state());
      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(115200);

  // Config relé
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  // Config servo PWM
  ledcSetup(SERVO_CH, SERVO_FREQ, SERVO_RES);
  ledcAttachPin(SERVO_PIN, SERVO_CH);
  moverServo(0); // Cerrar al inicio

  // WiFi + MQTT
  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(callback);

  // Sensor
  sensors.begin();
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  unsigned long now = millis();
  if (now - lastTempRead >= tempInterval) {
    lastTempRead = now;

    Serial.printf("📌 Referencia actual: %.2f\n", referencia);

    sensors.requestTemperatures();
    float tempC = sensors.getTempCByIndex(0);

    if (tempC == DEVICE_DISCONNECTED_C) {
      Serial.println("⚠️ Sensor desconectado o error de lectura.");
    } else {
      char payload[20];
      sprintf(payload, "%.2f", tempC);
      client.publish(topic, payload);
      Serial.printf("Temperatura publicada: %.2f °C\n", tempC);

      if (tempC > referencia) {
        digitalWrite(RELAY_PIN, LOW); // activar relé
        moverServo(180);              // abrir
        Serial.println("⚡ Relé ACTIVADO y servo ABIERTO");
      } else {
        digitalWrite(RELAY_PIN, HIGH); // apagar relé
        moverServo(0);                 // cerrar
        Serial.println("💤 Relé APAGADO y servo CERRADO");
      }
    }

    char payload2[20];
    sprintf(payload2, "%lu", now);
    client.publish(topic_millis, payload2);
    Serial.printf("Publicado millis(): %lu\n", now);
  }
}
