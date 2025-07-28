#include <WiFi.h>
#include <PubSubClient.h>

// Parámetros WiFi
const char* ssid     = "RedESP32";
const char* password = "claveesp32";

// Parámetros MQTT
const char* mqtt_server = "192.168.10.169"; // IP de tu PC con Mosquitto
const int mqtt_port = 1883;
const char* topic = "sensores/temperatura";

// Cliente WiFi y MQTT
WiFiClient espClient;
PubSubClient client(espClient);

// Función para conectar al WiFi
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

// Función para reconectar al broker MQTT
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

void setup() {
  Serial.begin(115200);
  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  // Temperatura simulada (puedes usar analogRead con sensor real)
  float temp = 25.0 + random(-10, 10) * 0.1; // simula variación

  // Convertir a string y publicar
  char payload[20];
  dtostrf(temp, 4, 2, payload);

  Serial.print("Publicando temperatura: ");
  Serial.println(payload);

  client.publish(topic, payload);

  delay(1000); // cada 1 segundo
}
