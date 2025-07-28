#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch, Host
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import Intf

def myNetwork():
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

    h1 = net.addHost('h1', cls=Host, ip='192.168.10.3/24', defaultRoute=None)    
    h6 = net.addHost('h6', cls=Host, ip='192.168.10.4/24', defaultRoute=None)        
    h2 = net.addHost('h2', cls=Host, ip='192.168.10.5/24', defaultRoute=None)    
    h3 = net.addHost('h3', cls=Host, ip='192.168.10.6/24', defaultRoute=None)    
    h4 = net.addHost('h4', cls=Host, ip='192.168.10.7/24', defaultRoute=None)    
    h5 = net.addHost('h5', cls=Host, ip='192.168.10.8/24', defaultRoute=None)       
    
    
   # Enlaces host-switch
    net.addLink(h1, s1)
    net.addLink(h6, s1)
    net.addLink(h2, s2)
    net.addLink(h3, s3)
    net.addLink(h4, s4)
    net.addLink(h5, s5)

    # Enlaces entre switches
    net.addLink(s1, s2)
    net.addLink(s1, s3)
    net.addLink(s1, s5)
    net.addLink(s2, s3)
    net.addLink(s3, s4)
    net.addLink(s4, s5)

    Intf('veth-ovs', node=s1)

    net.build()
    c0.start()

    s1.start([c0])
    s2.start([c0])
    s3.start([c0])
    s4.start([c0])
    s5.start([c0])

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    myNetwork()
