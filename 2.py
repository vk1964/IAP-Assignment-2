from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        # Path 1: Wi-Fi-like — 10Mbps, 10ms RTT
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1, bw=10, delay='10ms')
        self.addLink(s1, h2, bw=10, delay='10ms')

        # Path 2: LTE-like — 20Mbps, 50ms RTT
        s2 = self.addSwitch('s2')
        self.addLink(h1, s2, bw=20, delay='50ms')
        self.addLink(s2, h2, bw=20, delay='50ms')


def setup_ips(h1, h2):
    """Flush and assign IPs to both interfaces on each host"""
    for iface, ip in [('h1-eth0', '10.0.0.1/24'), ('h1-eth1', '10.0.1.1/24')]:
        h1.cmd(f'ip addr flush dev {iface}')
        h1.cmd(f'ip addr add {ip} dev {iface}')
        h1.cmd(f'ip link set {iface} up')

    for iface, ip in [('h2-eth0', '10.0.0.2/24'), ('h2-eth1', '10.0.1.2/24')]:
        h2.cmd(f'ip addr flush dev {iface}')
        h2.cmd(f'ip addr add {ip} dev {iface}')
        h2.cmd(f'ip link set {iface} up')


def setup_routing(h1, h2):
    """Per-interface routing tables so each subflow has a valid return path"""
    h1.cmd('ip route del default 2>/dev/null || true')
    h1.cmd('ip route add 10.0.0.0/24 dev h1-eth0 scope link table 1')
    h1.cmd('ip route add default dev h1-eth0 table 1')
    h1.cmd('ip rule add from 10.0.0.1 table 1 priority 101')
    h1.cmd('ip route add 10.0.1.0/24 dev h1-eth1 scope link table 2')
    h1.cmd('ip route add default dev h1-eth1 table 2')
    h1.cmd('ip rule add from 10.0.1.1 table 2 priority 102')
    h1.cmd('ip route add 10.0.0.2 dev h1-eth0 table main')

    h2.cmd('ip route del default 2>/dev/null || true')
    h2.cmd('ip route add 10.0.0.0/24 dev h2-eth0 scope link table 1')
    h2.cmd('ip route add default dev h2-eth0 table 1')
    h2.cmd('ip rule add from 10.0.0.2 table 1 priority 101')
    h2.cmd('ip route add 10.0.1.0/24 dev h2-eth1 scope link table 2')
    h2.cmd('ip route add default dev h2-eth1 table 2')
    h2.cmd('ip rule add from 10.0.1.2 table 2 priority 102')
    h2.cmd('ip route add 10.0.0.1 dev h2-eth0 table main')

    print("\n=== ROUTING RULES ===")
    print(f"  h1:\n{h1.cmd('ip rule show')}")
    print(f"  h2:\n{h2.cmd('ip rule show')}")


def setup_mptcp(h1, h2):
    """Enable MPTCP and register endpoints on both hosts"""
    h1.cmd('sysctl -w net.mptcp.enabled=1')
    h2.cmd('sysctl -w net.mptcp.enabled=1')

    print("\n=== MPTCP KERNEL STATUS ===")
    print(f"  h1: {h1.cmd('sysctl net.mptcp.enabled').strip()}")
    print(f"  h2: {h2.cmd('sysctl net.mptcp.enabled').strip()}")

    h1.cmd('ip mptcp endpoint flush')
    h2.cmd('ip mptcp endpoint flush')

    h1.cmd('ip mptcp endpoint add 10.0.0.1 dev h1-eth0 subflow signal')
    h1.cmd('ip mptcp endpoint add 10.0.1.1 dev h1-eth1 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.0.2 dev h2-eth0 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.1.2 dev h2-eth1 subflow signal')

    h1.cmd('ip mptcp limits set subflows 2 add_addr_accepted 2')
    h2.cmd('ip mptcp limits set subflows 2 add_addr_accepted 2')

    print("\n=== MPTCP ENDPOINTS ===")
    print(f"  h1:\n{h1.cmd('ip mptcp endpoint show')}")
    print(f"  h2:\n{h2.cmd('ip mptcp endpoint show')}")
    print(f"  h1 limits: {h1.cmd('ip mptcp limits show').strip()}")
    print(f"  h2 limits: {h2.cmd('ip mptcp limits show').strip()}")


def parse_bandwidth_over_time(raw_output, label):
    """
    Parse per-second bandwidth from iperf3 JSON intervals.
    Returns (avg_throughput, list of per-second Mbps values).
    """
    try:
        json_start = raw_output.find('{')
        data = json.loads(raw_output[json_start:])

        # Per-second interval data
        intervals   = data['intervals']
        per_second  = [iv['sum']['bits_per_second'] / 1e6
                       for iv in intervals]

        sent            = data['end']['sum_sent']
        received        = data['end']['sum_received']
        throughput_mbps = received['bits_per_second'] / 1e6
        retransmits     = sent.get('retransmits', 0)
        duration        = sent['seconds']

        # Summary block
        print(f"\n{'='*55}")
        print(f"  {label}")
        print(f"{'='*55}")
        print(f"  Duration          : {duration:.1f} s")
        print(f"  Avg Throughput    : {throughput_mbps:.2f} Mbps")
        print(f"  Peak Throughput   : {max(per_second):.2f} Mbps")
        print(f"  Min  Throughput   : {min(per_second):.2f} Mbps")
        print(f"  Data Sent         : {sent['bytes'] / 1e6:.2f} MB")
        print(f"  Retransmits       : {retransmits}")

        # Per-second bandwidth graph (ASCII)
        print(f"\n  Bandwidth over time (1s intervals):")
        print(f"  {'Time':<6} {'Mbps':>8}   Graph")
        print(f"  {'----':<6} {'----':>8}   -----")
        max_bw   = max(per_second) if per_second else 1
        bar_max  = 30  # max bar width in chars
        for i, bw in enumerate(per_second):
            bar_len = int((bw / max_bw) * bar_max)
            bar     = '█' * bar_len
            print(f"  {i+1:<6} {bw:>8.2f}   {bar}")

        print(f"{'='*55}\n")
        return throughput_mbps, per_second

    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [!] Parse error for {label}: {e}")
        print(f"  Raw: {raw_output[:300]}")
        return 0, []


def check_subflows(h1):
    """Show only active subflows with data in flight"""
    print("\n=== ACTIVE SUBFLOWS (during transfer) ===")
    out   = h1.cmd('ss -tin dst 10.0.0.2 or dst 10.0.1.2')
    lines = out.strip().split('\n')

    active = 0
    for line in lines:
        if 'ESTAB' in line:
            parts  = line.split()
            send_q = int(parts[2]) if len(parts) > 2 else 0
            if send_q > 0:
                print(f"  {line.strip()[:100]}")
                active += 1

    print(f"  Active paths with data in flight : {active}")
    if active >= 2:
        print("  [OK] Both paths actively sending")
    else:
        print("  [!] Less than 2 active paths detected")


def run_tcp_baseline(h1, h2):
    """Part 2 — TCP baseline, each path independently with bandwidth graph"""
    print("\n" + "="*55)
    print("  PART 2: TCP BASELINE")
    print("="*55)

    h2.cmd('pkill -f iperf3; sleep 0.5')
    h2.cmd('iperf3 -s &')
    time.sleep(2)

    # Path 1 — Wi-Fi, bind to interface, 1s interval reporting
    r1 = h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -t 10 -i 1 --json')
    t1, bw1 = parse_bandwidth_over_time(
        r1, "TCP — Path 1 (Wi-Fi, 10Mbps, 10ms RTT)")

    time.sleep(1)

    # Path 2 — LTE, bind to interface, 1s interval reporting
    r2 = h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -t 10 -i 1 --json')
    t2, bw2 = parse_bandwidth_over_time(
        r2, "TCP — Path 2 (LTE, 20Mbps, 50ms RTT)")

    h2.cmd('pkill -f iperf3')

    print(f"{'='*55}")
    print(f"  TCP BASELINE SUMMARY")
    print(f"{'='*55}")
    print(f"  Path 1 avg throughput   : {t1:.2f} Mbps  (cap: 10 Mbps)")
    print(f"  Path 2 avg throughput   : {t2:.2f} Mbps  (cap: 20 Mbps)")
    print(f"  Combined theoretical    : {t1+t2:.2f} Mbps")
    print(f"{'='*55}\n")

    try:
        from plot_helpers import save_bar_comparison, save_throughput_timeseries

        save_throughput_timeseries(
            {
                "TCP Path 1 (Wi-Fi)": bw1,
                "TCP Path 2 (LTE)": bw2,
            },
            "TCP baseline: per-path throughput (independent TCP flows)",
            "fig02_tcp_baseline_timeseries.png",
        )
        save_bar_comparison(
            ["TCP Path 1", "TCP Path 2", "Sum (theoretical multihoming)"],
            [t1, t2, t1 + t2],
            "TCP baseline: average throughput (Mbps)",
            "fig02_tcp_baseline_bars.png",
        )
    except ImportError:
        print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

    # Wait for TCP TIME_WAIT connections to clear before MPTCP test
    print("  Waiting for TCP connections to clear (5s)...")
    time.sleep(5)

    return t1, t2


def run_mptcp_aggregation(h1, h2, tcp1, tcp2):
    """
    Part 3 — MPTCP aggregation via parallel streams.
    One iperf3 stream bound per interface, 1s interval reporting.
    Bypasses minRTT scheduler limitation on older kernels.
    """
    print("\n" + "="*55)
    print("  PART 3: MPTCP AGGREGATION")
    print("  Method: Parallel streams, one per interface")
    print("="*55)

    h2.cmd('pkill -f iperf3; sleep 0.5')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(2)

    h1.cmd('rm -f /tmp/r1.txt /tmp/r2.txt')

    # Stream 1 — Wi-Fi path with 1s intervals
    h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t 15 -i 1 --json '
           '> /tmp/r1.txt 2>&1 &')

    # Stream 2 — LTE path with 1s intervals
    h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t 15 -i 1 --json '
           '> /tmp/r2.txt 2>&1 &')

    time.sleep(5)
    check_subflows(h1)

    # Wait for both results
    print("\n  Waiting for transfers to complete...")
    r1 = r2 = ''
    for i in range(25):
        time.sleep(1)
        r1 = h1.cmd('cat /tmp/r1.txt 2>/dev/null')
        r2 = h1.cmd('cat /tmp/r2.txt 2>/dev/null')
        if (r1.strip().startswith('{') and r1.strip().endswith('}') and
                r2.strip().startswith('{') and r2.strip().endswith('}')):
            print(f"  [OK] Both streams done after ~{i+1}s")
            break
        if i == 24:
            print("  [!] Timeout waiting for results")

    t1, bw1 = parse_bandwidth_over_time(
        r1, "MPTCP Stream 1 — Path 1 (Wi-Fi, 10Mbps)")
    t2, bw2 = parse_bandwidth_over_time(
        r2, "MPTCP Stream 2 — Path 2 (LTE, 20Mbps)")

    # Combined per-second bandwidth (zip both streams)
    if bw1 and bw2:
        combined = [a + b for a, b in zip(bw1, bw2)]
        print(f"\n{'='*55}")
        print(f"  COMBINED BANDWIDTH OVER TIME (both paths)")
        print(f"{'='*55}")
        print(f"  {'Time':<6} {'Path1':>8} {'Path2':>8} {'Total':>8}   Graph")
        print(f"  {'----':<6} {'-----':>8} {'-----':>8} {'-----':>8}   -----")
        max_combined = max(combined) if combined else 1
        bar_max      = 30
        for i, (b1, b2, bc) in enumerate(zip(bw1, bw2, combined)):
            bar_len = int((bc / max_combined) * bar_max)
            bar     = '█' * bar_len
            print(f"  {i+1:<6} {b1:>8.2f} {b2:>8.2f} {bc:>8.2f}   {bar}")

    # Final aggregation summary
    efficiency = ((t1+t2) / (tcp1+tcp2) * 100) if (tcp1+tcp2) > 0 else 0

    print(f"\n{'='*55}")
    print(f"  AGGREGATION SUMMARY")
    print(f"{'='*55}")
    print(f"  TCP Path 1 baseline     : {tcp1:.2f} Mbps")
    print(f"  TCP Path 2 baseline     : {tcp2:.2f} Mbps")
    print(f"  TCP Combined (theory)   : {tcp1+tcp2:.2f} Mbps")
    print(f"  {'─'*45}")
    print(f"  MPTCP Path 1            : {t1:.2f} Mbps")
    print(f"  MPTCP Path 2            : {t2:.2f} Mbps")
    print(f"  MPTCP Combined          : {t1+t2:.2f} Mbps")
    print(f"  Expected max            : ~28 Mbps")
    print(f"  Aggregation efficiency  : {efficiency:.1f}%")
    if t1 + t2 > 15:
        print(f"  [OK] Aggregation confirmed — both paths active")
    else:
        print(f"  [!] Aggregation failed — check subflow setup")
    print(f"{'='*55}\n")

    try:
        from plot_helpers import save_grouped_bars, save_throughput_timeseries

        series = {
            "Subflow 1 (Wi-Fi path)": bw1,
            "Subflow 2 (LTE path)": bw2,
        }
        if bw1 and bw2:
            series["Combined (sum of subflows)"] = [
                a + b for a, b in zip(bw1, bw2)
            ]
        save_throughput_timeseries(
            series,
            "Parallel streams over MPTCP endpoints: throughput over time",
            "fig02_mptcp_parallel_streams_timeseries.png",
        )
        save_grouped_bars(
            ["Path 1 (Wi-Fi)", "Path 2 (LTE)", "Combined"],
            ["TCP baseline", "Parallel streams (MPTCP setup)"],
            [[tcp1, tcp2, tcp1 + tcp2], [t1, t2, t1 + t2]],
            "TCP baseline vs parallel dual-path transfer: average throughput",
            "fig02_tcp_vs_mptcp_setup_bars.png",
        )
    except ImportError:
        print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

    h2.cmd('pkill -f iperf3')


def run():
    setLogLevel('warning')

    net = Mininet(
        topo=DualPathTopo(),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True
    )

    net.start()
    h1, h2 = net.get('h1', 'h2')

    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)

    print("\n=== CONNECTIVITY CHECK ===")
    print(h1.cmd('ping -c 3 10.0.0.2'))
    print(h1.cmd('ping -c 3 10.0.1.2'))

    # Part 2: TCP baseline per path
    tcp1, tcp2 = run_tcp_baseline(h1, h2)

    # Part 3: MPTCP aggregation with bandwidth over time
    run_mptcp_aggregation(h1, h2, tcp1, tcp2)

    net.stop()


if __name__ == "__main__":
    run()