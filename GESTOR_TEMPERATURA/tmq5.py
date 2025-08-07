from mfrc522 import SimpleMFRC522
import RPi.GPIO as GPIO
import time
import paho.mqtt.client as mqtt
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas

# -------------------- VARIABLES --------------------
modo = "visualizar"
temp_sensor = "N/A"
usuarios = {
    908469280906: "PABLO BERMEO",
    647386797817: "TYRONE NOVILLO"
}

# -------------------- MQTT --------------------
MQTT_BROKER = "192.168.10.169"
MQTT_PORT = 1883
TOPIC_SENSOR = "temp/sensor"
TOPIC_ID = "temp/id"
TOPIC_REF = "temp/ref"

client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Conectado con éxito.")
        client.subscribe(TOPIC_SENSOR)
    else:
        print(f"[MQTT] Error al conectar. Código: {rc}")

def on_message(client, userdata, msg):
    global temp_sensor
    try:
        temp_sensor = msg.payload.decode()
        print(f"[MQTT] Sensor recibido: {temp_sensor}")
    except Exception as e:
        print(f"[MQTT] Error decodificando mensaje: {e}")

def conectar_mqtt():
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT)
        client.loop_start()
        return True
    except Exception as e:
        print(f"[MQTT] Error de conexión: {e}")
        return False

# -------------------- OLED --------------------
serial = i2c(port=1, address=0x3C)
device = sh1106(serial, width=128, height=64)

def mostrar_mensaje(texto, tiempo=0, mostrar_check=False):
    with canvas(device) as draw:
        for i, linea in enumerate(texto.splitlines()):
            draw.text((5, 10 + i*15), linea, fill=255)
        if mostrar_check:
            draw.line((100, 45, 108, 53), fill=255, width=2)
            draw.line((108, 53, 120, 35), fill=255, width=2)
    if tiempo > 0:
        time.sleep(tiempo)

def mostrar_visualizacion():
    mostrar_mensaje(f"T. sensor:\n{temp_sensor} °C")

# -------------------- TECLADO --------------------
ROW_PINS = [4, 17,27,22]
COL_PINS = [5, 6, 13, 19]
KEYPAD = [
    ["1", "2", "3", "A"],
    ["4", "5", "6", "B"],
    ["7", "8", "9", "C"],
    ["*", "0", "#", "D"]
]

GPIO.setmode(GPIO.BCM)
for col in COL_PINS:
    GPIO.setup(col, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def get_key():
    for i, row_pin in enumerate(ROW_PINS):
        for r in ROW_PINS:
            GPIO.setup(r, GPIO.IN)
        GPIO.setup(row_pin, GPIO.OUT)
        GPIO.output(row_pin, GPIO.LOW)

        for j, col_pin in enumerate(COL_PINS):
            if GPIO.input(col_pin) == GPIO.LOW:
                return KEYPAD[i][j]
    return None

def leer_temperatura():
    while True:
        valor = ""
        last = None
        while True:
            with canvas(device) as draw:
                draw.text((0, 0), "Ingrese temp. ref:", fill=255)
                draw.text((10, 30), valor, fill=255)

            key = get_key()
            if key != last and key is not None:
                if key == "#":
                    break
                elif key == "*":
                    valor = ""
                elif key.isdigit():
                    valor += key
                else:
                    mostrar_mensaje("Caracter invalido", 2)
                    valor = ""
                    break
                time.sleep(0.3)
            elif key is None:
                last = None
            else:
                last = key
            time.sleep(0.05)

        if valor.isdigit():
            temp = int(valor)
            if 0 <= temp <= 100:
                return temp
            else:
                mostrar_mensaje("Fuera de rango", 2)
        else:
            mostrar_mensaje("Entrada invalida", 2)

def resetear_teclado():
    for r in ROW_PINS:
        GPIO.setup(r, GPIO.IN)
    for c in COL_PINS:
        GPIO.setup(c, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# -------------------- LOOP PRINCIPAL --------------------
try:
    mostrar_mensaje("Conectando a\nbroker MQTT...")
    if not conectar_mqtt():
        mostrar_mensaje("Error conexión\nMQTT", 3)
        raise Exception("No se pudo conectar")

    mostrar_mensaje("Conexión exitosa", 2)

    while True:
        if modo == "visualizar":
            mostrar_visualizacion()
            tecla = get_key()
            if tecla == "D":
                modo = "referencia"
                time.sleep(0.3)

        elif modo == "referencia":
            mostrar_mensaje("Aproxime tarjeta...")
            reader = SimpleMFRC522()
            uid = None
            timeout = time.time() + 10
            while uid is None and time.time() < timeout:
                uid, _ = reader.read_no_block()
                time.sleep(0.2)

            if uid is None or uid not in usuarios:
                mostrar_mensaje("UID no autorizado", 2)
                modo = "visualizar"
                continue

            nombre = usuarios[uid]
            mostrar_mensaje(f"Bienvenido\n{nombre}", 2, mostrar_check=True)

            temp = leer_temperatura()
            resetear_teclado()
            mostrar_mensaje(f"Ref. registrada:\n{temp} °C", 3)

            client.publish(TOPIC_ID, nombre)
            client.publish(TOPIC_REF, temp)
            print(f"[MQTT] ID publicado: {nombre}")
            print(f"[MQTT] Ref publicada: {temp}")

            modo = "visualizar"

        time.sleep(0.2)

except KeyboardInterrupt:
    print("Programa interrumpido por el usuario.")

finally:
    GPIO.cleanup()
    client.loop_stop()
