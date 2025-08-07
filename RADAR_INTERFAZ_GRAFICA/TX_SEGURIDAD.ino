#include <WiFi.h>
#include <WiFiUdp.h>

// --- Servo ---
#define SERVO_PIN 18
#define PWM_CH    0
#define PWM_FREQ  50
#define PWM_RES   16

#define PULSO_MIN 600
#define PULSO_MAX 2400
#define ANGULO_MIN 10
#define ANGULO_MAX 170

// --- HC-SR04 ---
#define TRIG_PIN 5
#define ECHO_PIN 19

// WiFi credentials
const char* ssid     = "s6";
const char* password = "claveesp32";

// UDP config
WiFiUDP udp;
const char* udpAddress = "192.168.10.108"; // IP del receptor UDP
const int udpPort = 2000;                 // Puerto del receptor UDP

void moverServo(int angulo) {
  int pulso = map(angulo, 0, 180, PULSO_MIN, PULSO_MAX);
  int duty = (pulso * 65535) / 20000;
  ledcWrite(PWM_CH, duty);
}

float medirDistancia() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duracion = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duracion == 0) return -1;

  return duracion * 0.0343 / 2;
}

void setup() {
  Serial.begin(9600);

  // Configura servo
  ledcSetup(PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(SERVO_PIN, PWM_CH);

  // Configura sensor
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  // Conecta WiFi
  Serial.print("Conectando a WiFi...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi conectado!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  // Inicia UDP
  udp.begin(udpPort);
}

void enviarUDP(String msg) {
  udp.beginPacket(udpAddress, udpPort);
  udp.print(msg);
  udp.endPacket();
  Serial.print("Enviado UDP: ");
  Serial.println(msg);
}

void loop() {
  for (int angulo = ANGULO_MIN; angulo <= ANGULO_MAX; angulo += 2) {
    moverServo(angulo);
    delay(150);
    float dist = medirDistancia();

    String mensaje = String(angulo) + "," + (dist > 0 ? String((int)dist) : "0") + ".";
    Serial.print(mensaje);
    enviarUDP(mensaje);
  }

  for (int angulo = ANGULO_MAX; angulo >= ANGULO_MIN; angulo -= 2) {
    moverServo(angulo);
    delay(150);
    float dist = medirDistancia();

    String mensaje = String(angulo) + "," + (dist > 0 ? String((int)dist) : "0") + ".";
    Serial.print(mensaje);
    enviarUDP(mensaje);
  }
}
