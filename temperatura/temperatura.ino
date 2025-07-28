#include <WiFi.h>
#include <PubSubClient.h>
#include <time.h>

// Parámetros WiFi
const char* ssid     = "RedESP32";
const char* password = "claveesp32";

// Parámetros MQTT
const char* mqtt_server = "192.168.10.169"; // IP de tu PC con Mosquitto
const int mqtt_port = 1883;
const char* topic = "sensor/temp"; // Este será usado para la hora

// Cliente WiFi y MQTT
WiFiClient espClient;
PubSubClient client(espClient);

// Parámetros NTP
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 0;             // Cambiar según zona horaria si hace falta
const int daylightOffset_sec = 0;

// Conexión a WiFi
void setup_wifi() {
  delay(100);
  Serial.print("Conectando a ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi conectado");
  Serial.print("IP local: ");
  Serial.println(WiFi.localIP());
}

// Conexión MQTT
void reconnect() {
  while (!client.connected()) {
    Serial.print("Intentando conexión MQTT...");
    if (client.connect("ESP32Client")) {
      Serial.println("conectado");
    } else {
      Serial.print("falló, rc=");
      Serial.print(client.state());
      Serial.println(" intentando de nuevo en 5s");
      delay(5000);
    }
  }
}

// Configurar sincronización NTP
void setupTime() {
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) {
    Serial.println("Error al obtener la hora NTP");
    return;
  }
  Serial.println(&timeinfo, "Hora NTP sincronizada: %Y-%m-%d %H:%M:%S");
}

void setup() {
  Serial.begin(115200);
  setup_wifi();
  setupTime();
  client.setServer(mqtt_server, mqtt_port);
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  // Obtener hora actual en formato string
  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    char timestamp[30];
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%d %H:%M:%S", &timeinfo);
    client.publish(topic, timestamp);
    Serial.print("Publicando hora: ");
    Serial.println(timestamp);
  } else {
    Serial.println("Error al obtener hora local");
  }

  delay(1000); // cada 1 segundo
}
