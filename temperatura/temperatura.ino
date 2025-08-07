#include <WiFi.h>
#include <PubSubClient.h>

// Parámetros WiFi
const char* ssid     = "s6";
const char* password = "claveesp32";

// Parámetros MQTT
const char* mqtt_server = "192.168.10.169"; // IP de tu PC con Mosquitto
const int mqtt_port = 1883;
const char* topic = "millis"; // Tópico MQTT

// Parámetros de IP estática
IPAddress local_IP(192, 168, 10, 230);
IPAddress gateway( 192, 168, 10,   1);
IPAddress subnet(  255, 255, 255,   0);


// Cliente WiFi y MQTT
WiFiClient espClient;
PubSubClient client(espClient);

// Conectar a WiFi
void setup_wifi() {
  delay(100);
  Serial.print("Conectando a ");
  Serial.println(ssid);

  // Configura IP estática
  if (!WiFi.config(local_IP, gateway, subnet)) {
    Serial.println("Error al configurar IP estática");
  }

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi conectado");
  Serial.print("IP local: ");
  Serial.println(WiFi.localIP());
}

// Reconectar a MQTT si se pierde conexión
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

  // Publicar millis() como timestamp
  unsigned long t_actual = millis();
  char payload[20];
  sprintf(payload, "%lu", t_actual);

  client.publish(topic, payload);
  Serial.print("Publicado millis(): ");
  Serial.println(payload);

  delay(1000); // cada segundo
}
