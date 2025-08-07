#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Controlador Ryu: iperf udp/5004 entre h1 (192.168.10.3) y h3 (192.168.10.6),
# IPTV, ARP, y MQTT (192.168.10.138 ↔ 192.168.10.169) vía S1-S5-S6.
#
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER,
    set_ev_cls
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ether_types
from ryu.lib import hub

# Umbral en bps (por ejemplo 100 Mbps)
UMBRAL_BPS = 5000


class Iperf5004WithARP(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    POLL_INTERVAL = 1  # segundos

    def __init__(self, *args, **kwargs):
        super(Iperf5004WithARP, self).__init__(*args, **kwargs)
        # tablas de aprendizaje y ARP
        self.mac_to_port = {}
        self.arp_table = {}
        # registro de datapaths vivos
        self.datapaths = {}
        # guardamos los byte_count anteriores por flujo (dpid, in_port)
        self.prev_flow_bytes = {}
        self.high_congestion = False
        # lanzar hilo de monitoreo de estadísticas
        self.monitor_thread = hub.spawn(self._monitor)

    #
    #  Registro y desregistro de switches al conectarse y desconectarse
    #
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if dp.id not in self.datapaths:
                self.logger.info("Registrando datapath %s", dp.id)
                self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            if dp.id in self.datapaths:
                self.logger.info("Eliminando datapath %s", dp.id)
                del self.datapaths[dp.id]
    
    #
    #  Configuración inicial de flujos (incluye ARP, IPTV, MQTT, etc.)
    #
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp     = ev.msg.datapath
        dpid   = dp.id
        ofp    = dp.ofproto
        parser = dp.ofproto_parser

        # Table-miss: enviar todo lo desconocido al controlador
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))

        # ARP: capturar todos los ARP
        match = parser.OFPMatch(eth_type=0x0806)
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=100, match=match, instructions=inst))


        # ----------------------------
        #   SWITCH S1  (DPID == 1)
        # ----------------------------
        if dpid == 1:
            
            # ------------------ IPTV entre VLAN 10 y VLAN 30 ------------------ #

            # --- IPTV saliente (hosts h1,h6,h5) → tag VLAN 10 y salida por puerto 4 → s2 ---
            for in_p in [1, 2, 3]:  # h1=s1-eth1, h6=s1-eth2, h5=s1-eth3
                match_iptv_out = parser.OFPMatch(
                    in_port=in_p,
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=17,               # UDP
                    udp_dst=5004               # puerto IPTV
                )
                actions_iptv_out = [
                    parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                    parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                    parser.OFPActionOutput(4)  # hacia s1-eth4 → enlace a s2
                ]

                dp.send_msg(parser.OFPFlowMod(
                    datapath=dp,
                    priority=30,               # más alta que otros flujos “normales”
                    match=match_iptv_out,
                    instructions=[parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions_iptv_out
                    )]
                ))


            # --- IPTV retorno: pop VLAN 30, ingress por s1-eth4, salida por s1-eth1,2,3 ---
            # 1) Crea un grupo ALL para el retorno IPTV (group_id = 20)
            buckets_iptv_ret_all = [
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(1)   # h1
                    ]
                ),
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(2)   # h6
                    ]
                ),
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(3)   # h5
                    ]
                )
            ]
            dp.send_msg(parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_ALL,
                group_id=20,
                buckets=buckets_iptv_ret_all
            ))


            # 2) FlowMod que usa el grupo ALL en lugar de una lista de OUTPUTs
            match_iptv_ret = parser.OFPMatch(
                in_port=4,
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 30),
                ip_proto=17,
                udp_src=5004
            )
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=30,
                match=match_iptv_ret,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=20)]
                )]
            ))

            # ----------- MQTT entre Mosquitto H5 y ESP32 ------------ #


            # 1) Crear grupo FF para el sentido ESP32→Mosquitto
            #    bucket1: si el enlace s1-eth6 (watch_port=6) está UP → salir por 3
            #    bucket2: si falla el anterior → salir por 3 vía enlace s1-eth7 (watch_port=7)
            buckets_fwd = [
                parser.OFPBucket(
                    watch_port=6,
                    actions=[parser.OFPActionOutput(3)]
                ),
                parser.OFPBucket(
                    watch_port=7,
                    actions=[parser.OFPActionOutput(3)]
                )
            ]
            req_fwd = parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_FF,
                group_id=1,
                buckets=buckets_fwd
            )
            dp.send_msg(req_fwd)

            # 2) Crear grupo FF para el sentido Mosquitto→ESP32
            #    bucket1: monitor s1-eth6 → mirar puerto 6 y, si está UP, salida por 6
            #    bucket2: si falla enlace 6 → salida por 7
            buckets_rev = [
                parser.OFPBucket(
                    watch_port=6,
                    actions=[parser.OFPActionOutput(6)]
                ),
                parser.OFPBucket(
                    watch_port=7,
                    actions=[parser.OFPActionOutput(7)]
                )
            ]
            req_rev = parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_FF,
                group_id=2,
                buckets=buckets_rev
            )
            dp.send_msg(req_rev)


            # 3a) ESP32→Mosquitto
            m_mqtt_fwd = parser.OFPMatch(
                in_port=6,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.138",
                ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=100,
                match=m_mqtt_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=1)]
                )]
            ))

            # 3b) Mosquitto→ESP32
            m_mqtt_rev = parser.OFPMatch(
                in_port=3,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.169",
                ipv4_dst="192.168.10.138",
                tcp_src=1883
            )
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=100,
                match=m_mqtt_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=2)]
                )]
            ))



            match_bkp_s1 = parser.OFPMatch(
                in_port=7,                            # enlace de backup s1←s6
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,                           # TCP
                ipv4_src="192.168.10.138",            # ESP32
                ipv4_dst="192.168.10.169",            # Mosquitto
                tcp_dst=1883                          # puerto MQTT
            )
            actions_bkp_s1 = [
                parser.OFPActionOutput(3)             # salida s1-eth3 (camino primario hacia S5)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=100,                         # misma prioridad que el flujo principal
                match=match_bkp_s1,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_bkp_s1
                )]
            ))

            # m_mqtt_fwd = parser.OFPMatch(
            #     in_port=6, eth_type=0x0800, ip_proto=6,
            #     ipv4_src="192.168.10.138", ipv4_dst="192.168.10.169",
            #     tcp_dst=1883
            # )
            # a_mqtt_fwd = [parser.OFPActionOutput(3)]
            # dp.send_msg(parser.OFPFlowMod(
            #     datapath=dp, priority=100, match=m_mqtt_fwd,
            #     instructions=[parser.OFPInstructionActions(
            #         ofp.OFPIT_APPLY_ACTIONS, a_mqtt_fwd
            #     )]
            # ))

            # m_mqtt_rev = parser.OFPMatch(
            #     in_port=3, eth_type=0x0800, ip_proto=6,
            #     ipv4_src="192.168.10.169", ipv4_dst="192.168.10.138",
            #     tcp_src=1883
            # )
            # a_mqtt_rev = [parser.OFPActionOutput(6)]
            # dp.send_msg(parser.OFPFlowMod(
            #     datapath=dp, priority=100, match=m_mqtt_rev,
            #     instructions=[parser.OFPInstructionActions(
            #         ofp.OFPIT_APPLY_ACTIONS, a_mqtt_rev
            #     )]
            # ))


            # --------- MQTT entre Mosquitto H5 y Raspberry ---------- #

            m_rasp_fwd = parser.OFPMatch(
                in_port=4, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.105", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            a_rasp_fwd = [parser.OFPActionOutput(3)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m_rasp_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_rasp_fwd
                )]
            ))
            
            m_rasp_rev = parser.OFPMatch(
                in_port=3, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            a_rasp_rev = [parser.OFPActionOutput(4)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m_rasp_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_rasp_rev
                )]
            ))


            # ------------- Trafico normal entre VLAN 10 y VLAN 30 ------------- #            

            # 1) Grupo SELECT para balancear 80/20 el tráfico VLAN10→VLAN30
            buckets = [
                # Bucket primario (80%): ruta s1→s5
                parser.OFPBucket(
                    weight=80,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(6)
                    ]
                ),
                # Bucket secundario (20%): ruta s1→s2
                parser.OFPBucket(
                    weight=20,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp.send_msg(parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_SELECT,
                group_id=10,
                buckets=buckets
            ))

            # 2) Flujos que usan el grupo SELECT 
            #    Todo tráfico IP que entra por 1,2,3 va al grupo 10
            for in_p in [1, 2, 3]:
                match = parser.OFPMatch(
                    in_port=in_p,
                    eth_type=ether_types.ETH_TYPE_IP
                )
                dp.send_msg(parser.OFPFlowMod(
                    datapath=dp,
                    priority=10,  
                    match=match,
                    instructions=[parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionGroup(group_id=10)]
                    )]
                ))

            # 3) Flujo de retorno VLAN30 que llega desde s2 (puerto 4)
            #    Pop VLAN y enviar a h1,h6,h5 (puertos 1,2,3)
            match_return = parser.OFPMatch(
                in_port=4,
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 30)
            )
            actions_return = [
                parser.OFPActionPopVlan(),
                parser.OFPActionOutput(1),
                parser.OFPActionOutput(2),
                parser.OFPActionOutput(3)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,
                match=match_return,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_return
                )]
            ))

             # --- Retorno VLAN 30: todo lo que entra por s1-eth6 sale a h5, h1 y h6 ---
            m_return = parser.OFPMatch(
                in_port=6,                             # viene de s1-eth6 (desde s5)
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 30)                 # etiqueta VLAN 30 presente
            )
            actions_return = [
                parser.OFPActionPopVlan(),             # quitamos la etiqueta antes de entregar a hosts
                parser.OFPActionOutput(1),             # h1 (s1-eth1)
                parser.OFPActionOutput(2),             # h6 (s1-eth2)
                parser.OFPActionOutput(3)              # h5 (s1-eth3)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                           # más alta que el DROP genérico
                match=m_return,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_return
                )]
            ))


            # ----------- UDP 2000 entre Radar y Visualizador ------------ #

            # --- Nuevo camino UDP/2000 entre 192.168.10.150 (AP-s6) → 192.168.10.108 (AP-s3) vía s1 ---
            # Sentido A: de s6 (in_port=7) hacia s3 (out_port=5)
            match_path_fwd = parser.OFPMatch(
                in_port=7,                            # s1-eth7 (viene de s6)
                eth_type=ether_types.ETH_TYPE_IP,     # IPv4
                ip_proto=17,                          # UDP
                ipv4_src="192.168.10.150",
                ipv4_dst="192.168.10.108",
                udp_dst=2000                          # puerto UDP 2000
            )
            actions_path_fwd = [
                parser.OFPActionOutput(5)             # s1-eth5 hacia s3
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,
                match=match_path_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_path_fwd
                )]
            ))

            # Sentido B: de s3 (in_port=5) de vuelta a s6 (out_port=7)
            match_path_rev = parser.OFPMatch(
                in_port=5,                            # s1-eth5 (viene de s3)
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=17,
                ipv4_src="192.168.10.108",
                ipv4_dst="192.168.10.150",
                udp_src=2000                          # tráfico de retorno UDP/2000
            )
            actions_path_rev = [
                parser.OFPActionOutput(7)             # s1-eth7 hacia s6
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,
                match=match_path_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_path_rev
                )]
            ))
            
            
                    

            # ----------------------- DROP ALL ----------------------- #

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return

        # ----------------------------
        #   SWITCH S2  (DPID == 2)
        # ----------------------------
        if dpid == 2:

            
            
            # ------------------ IPTV entre h1 y h3 ------------------ #

            # --- IPTV VLAN 10: ingress por s2-eth1 → egress por s2-eth2 ---
            match_iptv_s2 = parser.OFPMatch(
                in_port=1,                             # viene de s2-eth1 (desde s1)
                eth_type=ether_types.ETH_TYPE_IP,   # trama 802.1Q
                vlan_vid=(0x1000 | 10),                # VLAN ID = 10
                ip_proto=17,                           # UDP
                udp_dst=5004                           # puerto IPTV
            )
            actions_iptv_s2 = [
                parser.OFPActionOutput(2)              # hacia s2-eth2 (hacia s3)
            ]

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=30,                           # más alta que DROP
                match=match_iptv_s2,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_iptv_s2
                )]
            ))

            # --- IPTV bi-direccional retorno: VLAN 30 ingress por s2-eth2 → egress por s2-eth1 ---
            match_iptv_ret_s2 = parser.OFPMatch(
                in_port=2,                               # viene de s2-eth2 (desde s3)
                eth_type=ether_types.ETH_TYPE_IP,     # trama 802.1Q
                vlan_vid=(0x1000 | 30),                  # VLAN ID = 30
                ip_proto=17,                             # UDP
                udp_src=5004                             # tráfico IPTV de retorno
            )
            actions_iptv_ret_s2 = [
                parser.OFPActionOutput(1)                # hacia s2-eth1 (de regreso a s1)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=30,                             # más alta que DROP
                match=match_iptv_ret_s2,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_iptv_ret_s2
                )]
            ))
            

            # --------- MQTT entre Mosquitto H5 y Raspberry ---------- #

            m2_rasp_fwd = parser.OFPMatch(
                in_port=2, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.105", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            a2_rasp_fwd = [parser.OFPActionOutput(1)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m2_rasp_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a2_rasp_fwd
                )]
            ))
            
            m2_rasp_rev = parser.OFPMatch(
                in_port=1, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            a2_rasp_rev = [parser.OFPActionOutput(2)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m2_rasp_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a2_rasp_rev
                )]
            ))


            # ------------- Trafico normal entre VLAN 10 y VLAN 30 ------------- #    

            # --- VLAN 10: ingress por s2-eth1 → egress por s2-eth2 ---
            m_vlan10 = parser.OFPMatch(
                in_port=1,                            # s2-eth1
                eth_type=ether_types.ETH_TYPE_IP,     # trama 802.1Q
                vlan_vid=(0x1000 | 10)                # VLAN ID = 10
            )
            a_vlan10 = [parser.OFPActionOutput(2)]    # s2-eth2
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=20,
                match=m_vlan10,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_vlan10
                )]
            ))

            # --- VLAN 30: ingress por s2-eth2 → egress por s2-eth1 ---
            m_vlan30 = parser.OFPMatch(
                in_port=2,                            # s2-eth2
                eth_type=ether_types.ETH_TYPE_IP,     # trama 802.1Q
                vlan_vid=(0x1000 | 30)                # VLAN ID = 30
            )
            a_vlan30 = [parser.OFPActionOutput(1)]   # s2-eth1
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=20,
                match=m_vlan30,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_vlan30
                )]
            ))



            # ----------------------- DROP ALL ----------------------- #
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return
                

        # ----------------------------
        #   SWITCH S3  (DPID == 3)
        # ----------------------------
        if dpid == 3:
            
            # ------------------ IPTV entre h1 y h3 ------------------ #
            

            # 1) Crear grupo ALL para IPTV entrante en S3 (group_id = 21)
            buckets_iptv_in_all = [
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(1)   # h2 (s3-eth1)
                    ]
                ),
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(2)   # h3 (s3-eth2)
                    ]
                ),
                parser.OFPBucket(
                    actions=[
                        parser.OFPActionPopVlan(),
                        parser.OFPActionOutput(6)   # AP-s3 (s3-eth6)
                    ]
                )
            ]
            dp.send_msg(parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_ALL,
                group_id=21,
                buckets=buckets_iptv_in_all
            ))


            # --- IPTV VLAN 10 entrante por s3-eth4 → pop VLAN → salida a h2,h3,AP (puertos 1,2,6) ---
            match_iptv_in = parser.OFPMatch(
                in_port=4,                                # s3-eth4 conecta con s2
                eth_type=ether_types.ETH_TYPE_IP,         # trama 802.1Q
                vlan_vid=(0x1000 | 10),                   # VLAN ID = 10
                ip_proto=17,                              # UDP
                udp_dst=5004                              # puerto IPTV
            )
            
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=30,
                match=match_iptv_in,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=21)]
                )]
            ))

            # --- IPTV bi-direccional: tag VLAN 30 en tráfico UDP/5004 que entra por 1,2,6 y enviar por el puerto 4 ---
            for in_p in [1, 2, 6]:  # h2 (1), h3 (2), AP-s3 (6)
                match_iptv_bi = parser.OFPMatch(
                    in_port=in_p,
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=17,         # UDP
                    udp_src=5004         # tráfico IPTV de retorno
                )
                actions_iptv_bi = [
                    parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                    parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                    parser.OFPActionOutput(4)     # hacia s3-eth4 → enlace a s2
                ]
                dp.send_msg(parser.OFPFlowMod(
                    datapath=dp,
                    priority=30,
                    match=match_iptv_bi,
                    instructions=[parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions_iptv_bi
                    )]
                ))


            
            # --------- MQTT entre Mosquitto H5 y Raspberry ---------- #

            m3_rasp_fwd = parser.OFPMatch(
                in_port=6, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.105", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            a3_rasp_fwd = [parser.OFPActionOutput(4)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m3_rasp_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a3_rasp_fwd
                )]
            ))
            
            m3_rasp_rev = parser.OFPMatch(
                in_port=4, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            a3_rasp_rev = [parser.OFPActionOutput(6)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m3_rasp_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a3_rasp_rev
                )]
            ))


            # ------------- Trafico normal entre VLAN 30 y VLAN 10 ------------- #

            # 1) Crear grupo SELECT para retorno VLAN 30 con peso 60/40
            buckets_vlan30_return = [
                # 80% de los paquetes: tag VLAN30 + salida por s3-eth5 → puerto 5 (hacia S4)
                parser.OFPBucket(
                    weight=80,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(5)
                    ]
                ),
                # 20% de los paquetes: tag VLAN30 + salida por s3-eth4 → puerto 4 (ruta alternativa)
                parser.OFPBucket(
                    weight=20,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp.send_msg(parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_SELECT,
                group_id=30,
                buckets=buckets_vlan30_return
            ))

            # 2) Flujos de retorno que usan el grupo SELECT en lugar de OUTPUT directo
            for in_p in [1, 2, 6]:
                match_ret = parser.OFPMatch(
                    in_port=in_p,
                    eth_type=ether_types.ETH_TYPE_IP
                )
                dp.send_msg(parser.OFPFlowMod(
                    datapath=dp,
                    priority=10,
                    match=match_ret,
                    instructions=[parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionGroup(group_id=30)]
                    )]
                ))

            m_h6_all = parser.OFPMatch(
                in_port=5,                         # viene de s4-ethX
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 10)             # etiqueta VLAN 10 presente
            )
            actions_h6_all = [
                parser.OFPActionPopVlan(),
                parser.OFPActionOutput(1),         # hacia h2 (s3-eth1)
                parser.OFPActionOutput(2),         # hacia h3 (s3-eth2)
                parser.OFPActionOutput(6)          # hacia AP-s3
            ]

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                       # más alta que el DROP
                match=m_h6_all,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_h6_all
                )]
            ))

            m_h6_all = parser.OFPMatch(
                in_port=4,                         # viene de s2-eth1
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 10)             # etiqueta VLAN 10 presente
            )
            actions_h6_all = [
                parser.OFPActionPopVlan(),
                parser.OFPActionOutput(1),         # hacia h2 (s3-eth1)
                parser.OFPActionOutput(2),         # hacia h3 (s3-eth2)
                parser.OFPActionOutput(6)          # hacia AP-s3
            ]

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                       # más alta que el DROP
                match=m_h6_all,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_h6_all
                )]
            ))



            # ----------- UDP 2000 entre Radar y Visualizador ------------ #
            
            # --- UDP/2000 forward: de 192.168.10.150 → 192.168.10.108, ingress por s3-eth3 → egress por s3-eth6 (AP) ---
            match_path_fwd = parser.OFPMatch(
                in_port=3,                            # s3-eth3 (viene de s1)
                eth_type=ether_types.ETH_TYPE_IP,     # IPv4
                ip_proto=17,                          # UDP
                ipv4_src="192.168.10.150",
                ipv4_dst="192.168.10.108",
                udp_dst=2000                          # puerto UDP 2000
            )
            actions_path_fwd = [
                parser.OFPActionOutput(6)             # s3-eth6 hacia AP
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,
                match=match_path_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_path_fwd
                )]
            ))

            # --- UDP/2000 reverse: de 192.168.10.108 → 192.168.10.150, ingress por s3-eth6 → egress por s3-eth3 ---
            match_path_rev = parser.OFPMatch(
                in_port=6,                            # s3-eth6 (viene del AP)
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=17,
                ipv4_src="192.168.10.108",
                ipv4_dst="192.168.10.150",
                udp_src=2000                          # tráfico de retorno UDP/2000
            )
            actions_path_rev = [
                parser.OFPActionOutput(3)             # s3-eth3 de regreso hacia s1
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,
                match=match_path_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_path_rev
                )]
            ))




            # ----------------------- DROP ALL ----------------------- #
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return

        # ----------------------------
        #   SWITCH S4  (DPID == 4)
        # ----------------------------
        if dpid == 4:

            # ------------- Trafico normal entre H6 y H2 ------------- #

            m_h6_h2 = parser.OFPMatch(
                in_port=2,                          # viene de s4-eth2 (desde s5)
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=(0x1000 | 10)              # etiqueta VLAN 10 presente
            )

            a_h6_h2 = [
                parser.OFPActionOutput(1)           # hacia s4-eth1 (s3)
            ]

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                        # mayor que el DROP
                match=m_h6_h2,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_h6_h2
                )]
            ))



            # --- Retorno VLAN 30: todo lo que entra por el puerto 1 sale por el puerto 2 ---
            m_ret = parser.OFPMatch(
                in_port=1,                            # viene de s4-eth1 (h2/h3/AP)
                eth_type=ether_types.ETH_TYPE_IP,        # solo tráfico IP
                vlan_vid=(0x1000 | 30)                # etiqueta VLAN 30 presente
            )
            actions_ret = [
                parser.OFPActionOutput(2)             # hacia s4-eth2 (de regreso a s5)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                          # más alta que el DROP
                match=m_ret,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_ret
                )]
            ))




            # ----------------------- DROP ALL ----------------------- #
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return
        
        # ----------------------------
        #   SWITCH S5  (DPID == 5)
        # ----------------------------
        if dpid == 5:
            
            # ----------- MQTT entre Mosquitto H5 y ESP32 ------------ #

            m1 = parser.OFPMatch(
                in_port=3, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.138", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            a1 = [parser.OFPActionOutput(1)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m1,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a1
                )]
            ))

            m2 = parser.OFPMatch(
                in_port=1, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.138",
                tcp_src=1883
            )
            a2 = [parser.OFPActionOutput(3)]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m2,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a2
                )]
            ))


            # ------------- Trafico normal entre H6 y H2 ------------- #

            # Match: paquetes IPv4 entrantes por s5-eth1 (in_port=1) con VLAN ID 30
            m_h6_h2 = parser.OFPMatch(
                in_port=1,
                eth_type=0x0800,
                vlan_vid=(0x1000 | 10)     # el bit 0x1000 indica "VLAN present"
            )

            a_h6_h2 = [
                parser.OFPActionOutput(2)  # hacia s4 (s5-eth2)
            ]

            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,               # más alta que el DROP
                match=m_h6_h2,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, a_h6_h2
                )]
            ))


            # --- Retorno VLAN 30: todo lo que entra por el puerto 2 sale por el puerto 1 ---
            m_ret = parser.OFPMatch(
                in_port=2,                            # viene de s5-eth2 (desde s4)
                eth_type=ether_types.ETH_TYPE_IP,        # sólo tráfico IP
                vlan_vid=(0x1000 | 30)                # etiqueta VLAN 30 presente
            )
            actions_ret = [
                parser.OFPActionOutput(1)             # hacia s5-eth1 (de regreso a s1)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=10,                          # más alta que el DROP genérico
                match=m_ret,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_ret
                )]
            ))



            # ----------------------- DROP ALL ----------------------- #
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return

        # ---------------------------------
        #   SWITCH S6  (DPID == 6)
        # ---------------------------------
        if dpid == 6:

            # ----------- MQTT entre Mosquitto H5 y ESP32 ------------ #
            
            # 1) Crear grupo FF para ESP32→Mosquitto (in_port=4 → out_port=2 primario, backup→out_port=3)
            buckets_fwd_s6 = [
                parser.OFPBucket(
                    watch_port=2,
                    actions=[parser.OFPActionOutput(2)]
                ),
                parser.OFPBucket(
                    watch_port=3,
                    actions=[parser.OFPActionOutput(3)]
                )
            ]
            req_fwd_s6 = parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_FF,
                group_id=1,
                buckets=buckets_fwd_s6
            )
            dp.send_msg(req_fwd_s6)

            # 2) Crear grupo FF para Mosquitto→ESP32 (in_port=2 → out_port=4 primario, backup→out_port=3)
            buckets_rev_s6 = [
                parser.OFPBucket(
                    watch_port=2,
                    actions=[parser.OFPActionOutput(4)]
                ),
                parser.OFPBucket(
                    watch_port=3,
                    actions=[parser.OFPActionOutput(4)]
                )
            ]
            req_rev_s6 = parser.OFPGroupMod(
                datapath=dp,
                command=ofp.OFPGC_ADD,
                type_=ofp.OFPGT_FF,
                group_id=2,
                buckets=buckets_rev_s6
            )
            dp.send_msg(req_rev_s6)

            # 3) Flujos MQTT que usan los grupos en lugar de OUTPUT directo

            # ESP32→Mosquitto
            m_f = parser.OFPMatch(
                in_port=4, eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                ipv4_src="192.168.10.138", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m_f,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=1)]
                )]
            ))

            # Mosquitto→ESP32
            m_r = parser.OFPMatch(
                in_port=2, eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.138",
                tcp_src=1883
            )
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=100, match=m_r,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionGroup(group_id=2)]
                )]
            ))



            match_bkp_s6 = parser.OFPMatch(
                in_port=3,                            # enlace de backup s6←s1
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,                           # TCP
                ipv4_src="192.168.10.169",            # Mosquitto
                ipv4_dst="192.168.10.138",            # ESP32
                tcp_src=1883                          # puerto MQTT de origen
            )
            actions_bkp_s6 = [
                parser.OFPActionOutput(4)             # salida s6-eth4 (hacia ESP32)
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=100,
                match=match_bkp_s6,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_bkp_s6
                )]
            ))



            # m_f = parser.OFPMatch(
            #     in_port=4, eth_type=0x0800, ip_proto=6,
            #     ipv4_src="192.168.10.138", ipv4_dst="192.168.10.169",
            #     tcp_dst=1883
            # )
            # a_f = [parser.OFPActionOutput(2)]
            # dp.send_msg(parser.OFPFlowMod(
            #     datapath=dp, priority=100, match=m_f,
            #     instructions=[parser.OFPInstructionActions(
            #         ofp.OFPIT_APPLY_ACTIONS, a_f
            #     )]
            # ))

            # m_r = parser.OFPMatch(
            #     in_port=2, eth_type=0x0800, ip_proto=6,
            #     ipv4_src="192.168.10.169", ipv4_dst="192.168.10.138",
            #     tcp_src=1883
            # )
            # a_r = [parser.OFPActionOutput(4)]
            # dp.send_msg(parser.OFPFlowMod(
            #     datapath=dp, priority=100, match=m_r,
            #     instructions=[parser.OFPInstructionActions(
            #         ofp.OFPIT_APPLY_ACTIONS, a_r
            #     )]
            # ))



            # ----------- UDP 2000 entre Radar y Visualizador ------------ #

            # Sentido 1: 192.168.10.150 → 192.168.10.108
            match_fwd = parser.OFPMatch(
                in_port=4,                             # llega de s6-eth4 (AP-s6)
                eth_type=ether_types.ETH_TYPE_IP,      # IPv4
                ip_proto=17,                           # UDP
                ipv4_src="192.168.10.150",                 # host origen en s6
                ipv4_dst="192.168.10.108",             # destino en s3
                udp_dst=2000                           # puerto UDP 2000
            )
            actions_fwd = [
                parser.OFPActionOutput(3)              # sale por s6-eth3 hacia s1
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,                          # suficientemente alto
                match=match_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_fwd
                )]
            ))

            # Sentido 2: 192.168.10.108 → 192.168.10.150
            match_rev = parser.OFPMatch(
                in_port=3,                             # llega de s6-eth3 (vía s1)
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=17,
                ipv4_src="192.168.10.108",
                ipv4_dst="192.168.10.150",
                udp_src=2000
            )
            actions_rev = [
                parser.OFPActionOutput(4)              # sale por s6-eth4 hacia el AP
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                priority=200,
                match=match_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_rev
                )]
            ))

            
            # ----------------------- DROP ALL ----------------------- #
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=0,
                match=parser.OFPMatch(),
                instructions=[]
            ))
            return

    #
    #  Manejador de paquetes entrantes: L2 learning y ARP proxy
    #
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp  = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype != 0x0806:
            # L2 learning
            dpid = dp.id
            src = eth.src; dst = eth.dst
            self.mac_to_port.setdefault(dpid, {})
            self.mac_to_port[dpid][src] = in_port
            out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
            actions = [parser.OFPActionOutput(out_port)]
            out = parser.OFPPacketOut(
                datapath=dp, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions,
                data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
            )
            dp.send_msg(out)
            return

        # Proxy ARP
        arp_pkt = pkt.get_protocol(arp.arp)
        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip
        self.arp_table[src_ip] = (arp_pkt.src_mac, in_port)

        if arp_pkt.opcode == arp.ARP_REQUEST:
            if dst_ip in self.arp_table:
                dst_mac, _ = self.arp_table[dst_ip]
                # construir y enviar ARP reply
                arp_reply = packet.Packet()
                arp_reply.add_protocol(ethernet.ethernet(
                    ethertype=eth.ethertype,
                    dst=eth.src, src=dst_mac))
                arp_reply.add_protocol(arp.arp(
                    opcode=arp.ARP_REPLY,
                    src_mac=dst_mac, src_ip=dst_ip,
                    dst_mac=arp_pkt.src_mac, dst_ip=src_ip))
                arp_reply.serialize()
                actions = [parser.OFPActionOutput(in_port)]
                out = parser.OFPPacketOut(
                    datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=actions, data=arp_reply.data)
                dp.send_msg(out)
                return
            # flood si no conoce destino
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            out = parser.OFPPacketOut(
                datapath=dp, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=msg.data)
            dp.send_msg(out)
            return
        
    def _monitor(self):
        """Cada POLL_INTERVAL sonda en S1 y S3 todos los flujos IP de una vez."""
        while True:
            for dp in self.datapaths.values():
                if dp.id in [1, 3]:
                    parser = dp.ofproto_parser
                    # Pedimos stats de todos los flujos IP en la tabla 0
                    req = parser.OFPFlowStatsRequest(
                        dp,
                        table_id=0,
                        match=parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP)
                    )
                    dp.send_msg(req)
            hub.sleep(self.POLL_INTERVAL)

    
    
    def _set_groups_50_50(self):
        """Modifica grupos SELECT en S1 y S3 a 50/50."""
        # Switch S1: group_id=10
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp = dp1.ofproto; parser = dp1.ofproto_parser
            buckets = [
                parser.OFPBucket(
                    weight=50,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(6)
                    ]
                ),
                parser.OFPBucket(
                    weight=50,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp1.send_msg(parser.OFPGroupMod(
                datapath=dp1,
                command=ofp.OFPGC_MODIFY,
                type_=ofp.OFPGT_SELECT,
                group_id=10,
                buckets=buckets
            ))
        # Switch S3: group_id=30
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp = dp3.ofproto; parser = dp3.ofproto_parser
            buckets = [
                parser.OFPBucket(
                    weight=50,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(5)
                    ]
                ),
                parser.OFPBucket(
                    weight=50,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp3.send_msg(parser.OFPGroupMod(
                datapath=dp3,
                command=ofp.OFPGC_MODIFY,
                type_=ofp.OFPGT_SELECT,
                group_id=30,
                buckets=buckets
            ))


        # —————————————————————————————————————————————————————
        # 2) REROUTE MQTT-Raspberry
        # —————————————————————————————————————————————————————

        # --- En S1: modifica el flujo de retorno Mosquitto→Raspberry ---
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp = dp1.ofproto
            parser = dp1.ofproto_parser

            # FlowMod que reemplaza in_port=3 → output pasa de 4 a 5
            match_rev = parser.OFPMatch(
                in_port=3, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            actions_rev = [parser.OFPActionOutput(5)]
            dp1.send_msg(parser.OFPFlowMod(
                datapath=dp1,
                priority=100,
                match=match_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_rev
                )]
            ))

        # --- En S3: modifica el flujo de ida Raspberry→Mosquitto ---
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp = dp3.ofproto
            parser = dp3.ofproto_parser

            # FlowMod que reemplaza in_port=6 → output pasa de 4 a 3
            match_fwd = parser.OFPMatch(
                in_port=6, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.105", ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            actions_fwd = [parser.OFPActionOutput(3)]
            dp3.send_msg(parser.OFPFlowMod(
                datapath=dp3,
                priority=100,
                match=match_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_fwd
                )]
            ))

        # —— Reroute MQTT-Raspberry en S1 —— #
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp     = dp1.ofproto
            parser  = dp1.ofproto_parser
            # Cuando llega tráfico de la Raspberry por el puerto 5…
            match = parser.OFPMatch(
                in_port=5,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.105",    # Raspberry
                ipv4_dst="192.168.10.169",    # Mosquitto
                tcp_dst=1883
            )
            actions = [ parser.OFPActionOutput(3) ]  # salta directo por s1→s3
            dp1.send_msg(parser.OFPFlowMod(
                datapath=dp1,
                priority=100,
                match=match,
                instructions=[ parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions
                )]
            ))

        # —— Reroute MQTT-Raspberry en S3 —— #
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp     = dp3.ofproto
            parser  = dp3.ofproto_parser
            # Cuando llega tráfico de Mosquitto por el puerto 3…
            match = parser.OFPMatch(
                in_port=3,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.169",    # Mosquitto
                ipv4_dst="192.168.10.105",    # Raspberry
                tcp_src=1883
            )
            actions = [ parser.OFPActionOutput(6) ]  # sale por s3→AP (puerto 6)
            dp3.send_msg(parser.OFPFlowMod(
                datapath=dp3,
                priority=100,
                match=match,
                instructions=[ parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions
                )]
            ))
    
    def _set_groups_original(self):
        """Restaura grupos SELECT en S1 y S3 a 80/20 y 60/40."""
        # S1 (group 10) a 80/20
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp = dp1.ofproto; parser = dp1.ofproto_parser
            buckets = [
                parser.OFPBucket(
                    weight=80,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(6)
                    ]
                ),
                parser.OFPBucket(
                    weight=20,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 10)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp1.send_msg(parser.OFPGroupMod(
                datapath=dp1,
                command=ofp.OFPGC_MODIFY,
                type_=ofp.OFPGT_SELECT,
                group_id=10,
                buckets=buckets
            ))
        # S3 (group 30) a 60/40
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp = dp3.ofproto; parser = dp3.ofproto_parser
            buckets = [
                parser.OFPBucket(
                    weight=80,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(5)
                    ]
                ),
                parser.OFPBucket(
                    weight=20,
                    actions=[
                        parser.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                        parser.OFPActionSetField(vlan_vid=(0x1000 | 30)),
                        parser.OFPActionOutput(4)
                    ]
                )
            ]
            dp3.send_msg(parser.OFPGroupMod(
                datapath=dp3,
                command=ofp.OFPGC_MODIFY,
                type_=ofp.OFPGT_SELECT,
                group_id=30,
                buckets=buckets
            ))
    
        # —————————————————————————————————————————————————————
        # 2) RESTORE MQTT-Raspberry
        # —————————————————————————————————————————————————————

        # --- En S1: retorno Mosquitto→Raspberry: in_port=3 → output vuelve a 4 ---
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp = dp1.ofproto
            parser = dp1.ofproto_parser

            match_rev = parser.OFPMatch(
                in_port=3, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.169", ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            actions_rev = [parser.OFPActionOutput(4)]
            dp1.send_msg(parser.OFPFlowMod(
                datapath=dp1,
                priority=100,
                match=match_rev,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_rev
                )]
            ))

        # --- En S3: ida Raspberry→Mosquitto: in_port=6 → output vuelve a 4 ---
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp = dp3.ofproto
            parser = dp3.ofproto_parser

            match_fwd = parser.OFPMatch(
                in_port=6, eth_type=0x0800, ip_proto=6,
                ipv4_src="192.168.10.105", ipv4_dst="192.168.10.169",
                udp_dst=1883
            )
            actions_fwd = [parser.OFPActionOutput(4)]
            dp3.send_msg(parser.OFPFlowMod(
                datapath=dp3,
                priority=100,
                match=match_fwd,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions_fwd
                )]
            ))
        
        # —— Restaurar MQTT-Raspberry en S1 —— #
        dp1 = self.datapaths.get(1)
        if dp1:
            ofp    = dp1.ofproto
            parser = dp1.ofproto_parser
            # La ruta original Raspberry→Mosquitto entraba por el puerto 4…
            match = parser.OFPMatch(
                in_port=4,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.105",
                ipv4_dst="192.168.10.169",
                tcp_dst=1883
            )
            actions = [ parser.OFPActionOutput(3) ]
            dp1.send_msg(parser.OFPFlowMod(
                datapath=dp1,
                priority=100,
                match=match,
                instructions=[ parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions
                )]
            ))

        # —— Restaurar MQTT-Raspberry en S3 —— #
        dp3 = self.datapaths.get(3)
        if dp3:
            ofp    = dp3.ofproto
            parser = dp3.ofproto_parser
            # La ruta original Mosquitto→Raspberry entraba por el puerto 4…
            match = parser.OFPMatch(
                in_port=4,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_src="192.168.10.169",
                ipv4_dst="192.168.10.105",
                tcp_src=1883
            )
            actions = [ parser.OFPActionOutput(6) ]
            dp3.send_msg(parser.OFPFlowMod(
                datapath=dp3,
                priority=100,
                match=match,
                instructions=[ parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions
                )]
            ))




    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply(self, ev):
        """Procesa estadísticas de S1 y S3, sumando bytes de los puertos de interés."""
        dp = ev.msg.datapath
        dpid = dp.id

        # Sólo procesamos replies de S1 y S3
        if dpid not in [1, 3]:
            return

        total_bits = 0
        for stat in ev.msg.body:
            in_p = stat.match.get('in_port')
            # flujos de S1: in_ports 1,2,3
            if dpid == 1 and in_p in [1, 2, 3]:
                key = (dpid, in_p)
            # flujos de S3: in_ports 1,2,6
            elif dpid == 3 and in_p in [1, 2, 6]:
                key = (dpid, in_p)
            else:
                continue

            prev = self.prev_flow_bytes.get(key, 0)
            delta_bytes = stat.byte_count - prev
            self.prev_flow_bytes[key] = stat.byte_count
            total_bits += delta_bytes * 8

        # Calculamos bps agregados de S1+S3
        bps = total_bits / self.POLL_INTERVAL
        self.logger.info(
            "Bitrate trafico entre VLAN10↔30: %.2f bps", bps)

        if bps > UMBRAL_BPS and not self.high_congestion:
            self.logger.warning("¡Umbral sobrepasado, paso a 50/50!")
            self.high_congestion = True
            self._set_groups_50_50()

        elif bps <= UMBRAL_BPS and self.high_congestion:
            self.logger.info("Trafico normalizado, vuelvo a 80/20.")
            self.high_congestion = False
            self._set_groups_original()
            