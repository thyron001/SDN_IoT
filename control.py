# Copyright del autor original
# Licencia Apache 2.0

"""
Este módulo implementa un switch de nivel 2 con aprendizaje (L2 learning switch).
Se basa en un ejemplo de curso SDN. Es similar al módulo 'pyswitch' de NOX,
ya que instala reglas exactas para cada flujo detectado.
"""

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str, str_to_dpid, str_to_bool
from pox.lib.packet.tcp import tcp
from pox.lib.packet.ipv4 import ipv4
import time

# Obtener el logger para imprimir mensajes en consola
log = core.getLogger()

# Retardo inicial para evitar inundaciones al conectar el switch
_flood_delay = 0

class LearningSwitch (object):
  """
  Clase que implementa la lógica de un switch con aprendizaje.
  Permite asociar direcciones MAC con puertos y tomar decisiones de reenvío.
  """

  def __init__ (self, connection, transparent):
    # Conexión con el switch OpenFlow
    self.connection = connection
    # Si el switch es transparente (procesa o no tráfico LLDP)
    self.transparent = transparent
    # Tabla de aprendizaje MAC -> puerto
    self.macToPort = {}

    # Escuchar eventos provenientes de la conexión con el switch
    connection.addListeners(self)

    # Indica si se ha cumplido el tiempo de espera para inundación
    self.hold_down_expired = _flood_delay == 0

  def _handle_PacketIn (self, event):
    """
    Método principal que maneja paquetes entrantes (PacketIn) del switch.
    Se implementa el algoritmo de reenvío según la tabla de direcciones aprendidas.
    """

    packet = event.parsed  # Parsear el paquete recibido

    # Extraer información de los protocolos TCP e IPv4 si existen
    tcp_pkt = packet.find('tcp')
    ip_pkt = packet.find('ipv4')

    # ==== INGENIERÍA DE TRÁFICO: PRIORIZAR MQTT (TCP 1883) ====
    if tcp_pkt is not None and ip_pkt is not None:
        if tcp_pkt.dstport == 1883 or tcp_pkt.srcport == 1883:
            # Si es tráfico MQTT, buscar el puerto de destino conocido
            dst_port = self.macToPort.get(packet.dst)
            if dst_port is not None and dst_port != event.port:
                log.info("Tráfico MQTT detectado: %s:%s -> %s:%s por puerto %s" % (
                    ip_pkt.srcip, tcp_pkt.srcport, ip_pkt.dstip, tcp_pkt.dstport, dst_port))

                # Crear una regla de flujo con alta prioridad para el tráfico MQTT
                msg = of.ofp_flow_mod()
                msg.priority = 50000  # Prioridad alta
                msg.match = of.ofp_match.from_packet(packet, event.port)
                msg.idle_timeout = 20
                msg.hard_timeout = 60
                msg.actions.append(of.ofp_action_output(port=dst_port))
                msg.data = event.ofp
                self.connection.send(msg)
                return  # Terminar el procesamiento aquí
            else:
                # Si no se conoce el puerto de destino aún, se inunda el paquete
                flood("Tráfico MQTT sin puerto conocido — flooding")
                return

    # ==== FUNCIONES INTERNAS ====

    def flood (message = None):
      """
      Función para inundar el paquete por todos los puertos excepto el de entrada.
      Utilizada cuando no se conoce el puerto destino.
      """
      msg = of.ofp_packet_out()
      if time.time() - self.connection.connect_time >= _flood_delay:
        if self.hold_down_expired is False:
          self.hold_down_expired = True
          log.info("%s: Fin del retardo de inundación — se permite flooding",
              dpid_to_str(event.dpid))

        if message is not None:
          log.debug(message)

        # Acción: enviar a todos los puertos (excepto el entrante)
        msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
      msg.data = event.ofp
      msg.in_port = event.port
      self.connection.send(msg)

    def drop (duration = None):
      """
      Función para descartar paquetes. También puede instalar una regla temporal
      para descartar flujos similares por un tiempo.
      """
      if duration is not None:
        if not isinstance(duration, tuple):
          duration = (duration, duration)
        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match.from_packet(packet)
        msg.idle_timeout = duration[0]
        msg.hard_timeout = duration[1]
        msg.buffer_id = event.ofp.buffer_id
        self.connection.send(msg)
      elif event.ofp.buffer_id is not None:
        msg = of.ofp_packet_out()
        msg.buffer_id = event.ofp.buffer_id
        msg.in_port = event.port
        self.connection.send(msg)

    # ==== ALGORITMO DE SWITCH L2 ====

    # 1) Aprender la dirección MAC de origen y el puerto por el que llegó
    self.macToPort[packet.src] = event.port

    # 2) Filtrar tráfico LLDP y de direcciones bridge (si no es transparente)
    if not self.transparent:
      if packet.type == packet.LLDP_TYPE or packet.dst.isBridgeFiltered():
        drop()  # 2a) Se descarta el paquete
        return

    # 3) Si el destino es una dirección multicast, se inunda
    if packet.dst.is_multicast:
      flood()  # 3a
    else:
      # 4) Si no se conoce el puerto del destino, se inunda
      if packet.dst not in self.macToPort:
        flood("Puerto de destino %s desconocido — flooding" % (packet.dst,))
      else:
        port = self.macToPort[packet.dst]
        # 5) Si el puerto destino es el mismo por donde llegó el paquete, se descarta
        if port == event.port:
          log.warning("Mismo puerto de entrada y salida: %s -> %s en %s.%s. Se descarta." %
              (packet.src, packet.dst, dpid_to_str(event.dpid), port))
          drop(10)  # Se descartan flujos similares por 10 segundos
          return

        # 6) Se instala una regla de flujo para que este tráfico se reenvíe correctamente
        log.debug("Instalando flujo %s.%i -> %s.%i" %
                  (packet.src, event.port, packet.dst, port))
        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match.from_packet(packet, event.port)
        msg.idle_timeout = 10
        msg.hard_timeout = 30
        msg.actions.append(of.ofp_action_output(port = port))
        msg.data = event.ofp  # 6a) Se reenvía este paquete también
        self.connection.send(msg)


class l2_learning (object):
  """
  Clase que escucha conexiones de switches y les asigna la lógica de LearningSwitch.
  """

  def __init__ (self, transparent, ignore = None):
    """
    Constructor. Inicializa el modo transparente y lista de switches ignorados.
    """
    core.openflow.addListeners(self)
    self.transparent = transparent
    self.ignore = set(ignore) if ignore else ()

  def _handle_ConnectionUp (self, event):
    """
    Evento que se ejecuta cuando un switch se conecta al controlador.
    Si no está en la lista de ignorados, se le asigna un LearningSwitch.
    """
    if event.dpid in self.ignore:
      log.debug("Ignorando conexión %s" % (event.connection,))
      return
    log.debug("Conexión recibida: %s" % (event.connection,))
    LearningSwitch(event.connection, self.transparent)


def launch (transparent=False, hold_down=_flood_delay, ignore = None):
  """
  Función que lanza el controlador desde línea de comandos POX.
  Permite configurar si el switch es transparente, el retardo de inundación y switches ignorados.
  """
  try:
    global _flood_delay
    _flood_delay = int(str(hold_down), 10)
    assert _flood_delay >= 0
  except:
    raise RuntimeError("El parámetro hold-down debe ser un número")

  if ignore:
    ignore = ignore.replace(',', ' ').split()
    ignore = set(str_to_dpid(dpid) for dpid in ignore)

  # Registrar la aplicación l2_learning en el núcleo de POX
  core.registerNew(l2_learning, str_to_bool(transparent), ignore)
