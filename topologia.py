#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch, Host
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import Intf
import os          

def myNetwork():
    
    os.system("sudo ip link add veth-eth0 type veth peer name veth-ovs1")
    os.system("sudo ip link set veth-eth0 up")
    os.system("sudo ip link set veth-ovs1 up")
    os.system("sudo ip link set dev veth-eth0 master br1")
    os.system("sudo ip link set dev eth0 master br1")

    net = Mininet(topo=None, build=False, ipBase='10.0.0.0/8')

    c0 = net.addController('c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6633
    )

    s1 = net.addSwitch('s1', cls=OVSKernelSwitch)
    s2 = net.addSwitch('s2', cls=OVSKernelSwitch)
    s3 = net.addSwitch('s3', cls=OVSKernelSwitch)
    s4 = net.addSwitch('s4', cls=OVSKernelSwitch)
    s5 = net.addSwitch('s5', cls=OVSKernelSwitch)
    s6 = net.addSwitch('s6', cls=OVSKernelSwitch)
    
    h1 = net.addHost('h1', cls=Host, ip='192.168.10.3/24'  , defaultRoute=None)    
    h6 = net.addHost('h6', cls=Host, ip='192.168.10.4/24'  , defaultRoute=None)        
    h2 = net.addHost('h2', cls=Host, ip='192.168.10.5/24'  , defaultRoute=None)    
    h3 = net.addHost('h3', cls=Host, ip='192.168.10.6/24'  , defaultRoute=None)    
    h4 = net.addHost('h4', cls=Host, ip='192.168.10.7/24'  , defaultRoute=None)    
    h5 = net.addHost('h5', cls=Host, ip='192.168.10.169/24', defaultRoute=None)       
    
   # Enlaces host-switch
    net.addLink(h1, s1)
    net.addLink(h6, s1)
    net.addLink(h5, s1)
    net.addLink(h2, s3)
    net.addLink(h3, s3)
    net.addLink(h4, s6)

    # Enlaces entre switches
    net.addLink(s1, s2)
    net.addLink(s1, s3)
    net.addLink(s1, s5)
    net.addLink(s2, s3)
    net.addLink(s3, s4)
    net.addLink(s4, s5)
    net.addLink(s5, s6)
    net.addLink(s1, s6)

    Intf('veth-ovs', node=s6)
    Intf('veth-ovs1', node=s3)

    net.build()
    c0.start()

    s1.start([c0])
    s2.start([c0])
    s3.start([c0])
    s4.start([c0])
    s5.start([c0])
    s6.start([c0])


    # # ——————————————————————————
    # # Configurar puertos de acceso VLAN para hosts
    # # ——————————————————————————
    # access_ports = [
    #     (s1, 's1-eth1', 100),  # h1 en VLAN100
    #     (s1, 's1-eth2', 100),  # h6 en VLAN100
    #     (s1, 's1-eth3', 100),  # h5 en VLAN100
    #     (s3, 's3-eth1', 300),  # h2 en VLAN300
    #     (s3, 's3-eth2', 300),  # h3 en VLAN300
    #     (s6, 's6-eth1', 600),  # h4 en VLAN600
    # ]
    # for sw, port, vlan in access_ports:
    #     sw.cmd(f'ovs-vsctl set port {port} vlan_mode=access tag={vlan}')

    # # ——————————————————————————
    # # Configurar troncales VLAN100 y VLAN300
    # # ——————————————————————————
    # trunk_ports = [
    #     (s1, 's1-eth4'), (s2, 's2-eth1'),
    #     (s1, 's1-eth5'), (s3, 's3-eth3'),
    #     (s1, 's1-eth6'), (s5, 's5-eth1'),
    #     (s1, 's1-eth7'), (s6, 's6-eth3'),
    #     (s2, 's2-eth2'), (s3, 's3-eth4'),
    #     (s3, 's3-eth5'), (s4, 's4-eth1'),
    #     (s4, 's4-eth2'), (s5, 's5-eth2'),
    #     (s5, 's5-eth3'), (s6, 's6-eth2'),
    # ]
    # for sw, port in trunk_ports:
    #     sw.cmd(f'ovs-vsctl set port {port} vlan_mode=trunk trunks=100,300')

    
    #h5.cmd('mosquitto -c /etc/mosquitto/mosquitto.conf -d')

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    myNetwork()