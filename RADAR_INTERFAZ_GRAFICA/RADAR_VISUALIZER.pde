import hypermedia.net.*;

//import udp.*; // Librería UDP de Stephane Cousot

// --- UDP ---
UDP udp;
String angle = "";
String distance = "";
String data = "";
String noObject;
float pixsDistance;
int iAngle, iDistance;
int index1 = 0;
PFont orcFont;

void setup() {
  size(1360, 768); // Tu resolución de pantalla
  smooth();

  // Puerto donde escucha Processing (debe coincidir con el destino de la ESP32)
  udp = new UDP(this, 12345);
  udp.listen(true);
}

void draw() {
  fill(98, 245, 31);
  noStroke();
  fill(0, 4);
  rect(0, 0, width, height - height * 0.065);
  fill(98, 245, 31);

  drawRadar();
  drawLine();
  drawObject();
  drawText();
}

void receive(byte[] dataRaw, String ip, int port) {
  // Convertir bytes a string
  data = new String(dataRaw).trim();

  // Buscar coma separadora
  index1 = data.indexOf(",");
  if (index1 > 0) {
    angle = data.substring(0, index1);
    distance = data.substring(index1 + 1, data.length() - 1); // quitar el punto final
    iAngle = int(angle);
    iDistance = int(distance);
  }
}

// ---- Dibujo del radar ----
void drawRadar() {
  pushMatrix();
  translate(width / 2, height - height * 0.074);
  noFill();
  strokeWeight(2);
  stroke(98, 245, 31);

  // Arcos para 100 cm (divididos en 4)
  arc(0, 0, (width - width * 0.0625), (width - width * 0.0625), PI, TWO_PI);
  arc(0, 0, (width - width * 0.27), (width - width * 0.27), PI, TWO_PI);
  arc(0, 0, (width - width * 0.479), (width - width * 0.479), PI, TWO_PI);
  arc(0, 0, (width - width * 0.687), (width - width * 0.687), PI, TWO_PI);

  // Líneas de ángulo
  line(-width / 2, 0, width / 2, 0);
  line(0, 0, (-width / 2) * cos(radians(30)), (-width / 2) * sin(radians(30)));
  line(0, 0, (-width / 2) * cos(radians(60)), (-width / 2) * sin(radians(60)));
  line(0, 0, (-width / 2) * cos(radians(90)), (-width / 2) * sin(radians(90)));
  line(0, 0, (-width / 2) * cos(radians(120)), (-width / 2) * sin(radians(120)));
  line(0, 0, (-width / 2) * cos(radians(150)), (-width / 2) * sin(radians(150)));
  line((-width / 2) * cos(radians(30)), 0, width / 2, 0);
  popMatrix();
}

void drawObject() {
  pushMatrix();
  translate(width / 2, height - height * 0.074);
  strokeWeight(9);
  stroke(255, 10, 10);

  pixsDistance = iDistance * ((height - height * 0.1666) * 0.01); // Escala para 100 cm

  // Limitar a 100 cm
  if (iDistance < 100) {
    line(pixsDistance * cos(radians(iAngle)),
         -pixsDistance * sin(radians(iAngle)),
         (width - width * 0.505) * cos(radians(iAngle)),
         -(width - width * 0.505) * sin(radians(iAngle)));
  }
  popMatrix();
}

void drawLine() {
  pushMatrix();
  strokeWeight(9);
  stroke(30, 250, 60);
  translate(width / 2, height - height * 0.074);
  line(0, 0,
       (height - height * 0.12) * cos(radians(iAngle)),
       -(height - height * 0.12) * sin(radians(iAngle)));
  popMatrix();
}

void drawText() {
  pushMatrix();
  if (iDistance > 100) {
    noObject = "Out of Range";
  } else {
    noObject = "In Range";
  }
  fill(0, 0, 0);
  noStroke();
  rect(0, height - height * 0.0648, width, height);
  fill(98, 245, 31);
  textSize(25);
  text("25cm", width - width * 0.3854, height - height * 0.0833);
  text("50cm", width - width * 0.281, height - height * 0.0833);
  text("75cm", width - width * 0.177, height - height * 0.0833);
  text("100cm", width - width * 0.0729, height - height * 0.0833);
  textSize(40);
  text("Radar UDP", width - width * 0.875, height - height * 0.0277);
  text("Ángulo: " + iAngle + " °", width - width * 0.48, height - height * 0.0277);
  text("Dist:", width - width * 0.26, height - height * 0.0277);
  if (iDistance <= 100) {
    text(" " + iDistance + " cm", width - width * 0.225, height - height * 0.0277);
  }
  popMatrix();
}
