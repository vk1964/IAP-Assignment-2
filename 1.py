from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)  
        h2 = self.addHost('h2', ip=None)

        # Path 1: Wi-Fi-like — lower bandwidth, low RTT
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1, bw=10, delay='10ms')
        self.addLink(s1, h2, bw=10, delay='10ms')

        # Path 2: LTE-like — higher bandwidth, higher RTT
        s2 = self.addSwitch('s2')
        self.addLink(h1, s2, bw=20, delay='50ms')
        self.addLink(s2, h2, bw=20, delay='50ms')


def setup_interfaces(h1, h2):
    """Assign IPs to both interfaces on each host"""

    # h1: two interfaces for two paths
    h1.cmd('ip addr flush dev h1-eth0')
    h1.cmd('ip addr flush dev h1-eth1')
    h1.cmd('ifconfig h1-eth0 10.0.1.1/24')
    h1.cmd('ifconfig h1-eth1 10.0.2.1/24')

    # h2: two interfaces for two paths
    h2.cmd('ip addr flush dev h2-eth0')
    h2.cmd('ip addr flush dev h2-eth1')
    h2.cmd('ifconfig h2-eth0 10.0.1.2/24')
    h2.cmd('ifconfig h2-eth1 10.0.2.2/24')

    # Routing: each interface uses its own subnet gateway
    h1.cmd('ip route add 10.0.1.0/24 dev h1-eth0')
    h1.cmd('ip route add 10.0.2.0/24 dev h1-eth1')
    h2.cmd('ip route add 10.0.1.0/24 dev h2-eth0')
    h2.cmd('ip route add 10.0.2.0/24 dev h2-eth1')


import json

def parse_iperf_result(raw_output, label):
    """Parse iperf3 JSON output and print a clean summary.
    Returns (avg_mbps, per_second_mbps_list) for plotting; (0, []) on failure.
    """
    try:
        json_start = raw_output.find('{')
        data = json.loads(raw_output[json_start:])

        sent     = data['end']['sum_sent']
        received = data['end']['sum_received']
        streams  = data['start']['connected']

        throughput_mbps = received['bits_per_second'] / 1e6
        retransmits     = sent.get('retransmits', 0)
        duration        = sent['seconds']
        per_sec         = [
            iv['sum']['bits_per_second'] / 1e6 for iv in data.get('intervals', [])
        ]

        print(f"\n{'='*45}")
        print(f"  {label}")
        print(f"{'='*45}")
        print(f"  Duration     : {duration:.1f} s")
        print(f"  Throughput   : {throughput_mbps:.2f} Mbps")
        print(f"  Data Sent    : {sent['bytes'] / 1e6:.2f} MB")
        print(f"  Retransmits  : {retransmits}")
        print(f"{'='*45}\n")
        return throughput_mbps, per_sec

    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [!] Could not parse result for {label}: {e}")
        print(f"  Raw output: {raw_output[:300]}")
        return 0.0, []


def run_tcp_baseline(h1, h2):
    h2.cmd('pkill -f iperf3; sleep 0.5')
    h2.cmd('iperf3 -s -D')
    time.sleep(1)

    result1 = h1.cmd('iperf3 -c 10.0.1.2 -t 10 -i 1 --json')
    t1, bw1 = parse_iperf_result(
        result1, "PATH 1 — Wi-Fi-like (10ms RTT, 10Mbps)")

    time.sleep(1)

    result2 = h1.cmd('iperf3 -c 10.0.2.2 -t 10 -i 1 --json')
    t2, bw2 = parse_iperf_result(
        result2, "PATH 2 — LTE-like (50ms RTT, 20Mbps)")

    h2.cmd('pkill -f iperf3')

    try:
        from plot_helpers import save_bar_comparison, save_throughput_timeseries

        if t1 > 0 or t2 > 0:
            save_bar_comparison(
                ["Wi-Fi-like path\n(10 Mbps cap)", "LTE-like path\n(20 Mbps cap)"],
                [t1, t2],
                "Baseline TCP: average throughput per path (independent flows)",
                "fig01_tcp_baseline_avg_throughput.png",
            )
        if bw1 or bw2:
            save_throughput_timeseries(
                {
                    "Path 1 — Wi-Fi-like (10 Mbps, ~20 ms RTT)": bw1,
                    "Path 2 — LTE-like (20 Mbps, ~100 ms RTT)": bw2,
                },
                "Baseline TCP: throughput over time (separate runs)",
                "fig01_tcp_baseline_timeseries.png",
            )
    except ImportError:
        print("  [!] plot_helpers / matplotlib not available — skip PNG figures")


 
def run():
    setLogLevel('error')

    net = Mininet(
        topo=DualPathTopo(),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True
    )

    net.start()

    h1, h2 = net.get('h1', 'h2')
    setup_interfaces(h1, h2)

    print("\n=== CONNECTIVITY CHECK ===")
    h1.cmd('ping -c 3 10.0.1.2')
    h1.cmd('ping -c 3 10.0.2.2')

    run_tcp_baseline(h1, h2)

    net.stop()


if __name__ == "__main__":
    run()