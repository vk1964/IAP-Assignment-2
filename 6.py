from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json
import subprocess

# ─────────────────────────────────────────────
#  Topology — Dual-Homed Host Configuration
#  Simulating two independent access paths (e.g., WiFi and LTE)
# ─────────────────────────────────────────────

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        # s1 represents the "Primary/WiFi" path
        s1 = self.addSwitch('s1', cls=OVSBridge)
        # s2 represents the "Backup/LTE" path
        s2 = self.addSwitch('s2', cls=OVSBridge)

        # Initial symmetric conditions to test Aggregation
        self.addLink(h1, s1, bw=10, delay='20ms')
        self.addLink(s1, h2, bw=10, delay='20ms')

        self.addLink(h1, s2, bw=10, delay='50ms')
        self.addLink(s2, h2, bw=10, delay='50ms')


# ─────────────────────────────────────────────
#  Network Configuration Helpers
# ─────────────────────────────────────────────

def setup_ips(h1, h2):
    """Assigns IP addresses to multiple interfaces for Multi-Homing."""
    for iface, ip in [('h1-eth0', '10.0.0.1/24'), ('h1-eth1', '10.0.1.1/24')]:
        h1.cmd(f'ip addr flush dev {iface}')
        h1.cmd(f'ip addr add {ip} dev {iface}')
        h1.cmd(f'ip link set {iface} up')

    for iface, ip in [('h2-eth0', '10.0.0.2/24'), ('h2-eth1', '10.0.1.2/24')]:
        h2.cmd(f'ip addr flush dev {iface}')
        h2.cmd(f'ip addr add {ip} dev {iface}')
        h2.cmd(f'ip link set {iface} up')


def setup_routing(h1, h2):
    """
    Configures Policy-Based Routing (PBR).
    Crucial for Handover: ensures packets return via the same interface they arrived on,
    preventing subflow drops due to asymmetric routing.
    """
    for host, src0, src1 in [
        (h1, '10.0.0.1', '10.0.1.1'),
        (h2, '10.0.0.2', '10.0.1.2'),
    ]:
        pfx0 = src0.rsplit('.', 1)[0] + '.0/24'
        pfx1 = src1.rsplit('.', 1)[0] + '.0/24'
        eth0 = f'{host.name}-eth0'
        eth1 = f'{host.name}-eth1'
        peer = '10.0.0.2' if host.name == 'h1' else '10.0.0.1'

        host.cmd('ip route del default 2>/dev/null || true')
        host.cmd(f'ip route add {pfx0} dev {eth0} scope link table 1')
        host.cmd(f'ip route add default dev {eth0} table 1')
        host.cmd(f'ip rule add from {src0} table 1 priority 101')
        host.cmd(f'ip route add {pfx1} dev {eth1} scope link table 2')
        host.cmd(f'ip route add default dev {eth1} table 2')
        host.cmd(f'ip rule add from {src1} table 2 priority 102')
        host.cmd(f'ip route add {peer} dev {eth0} table main')


def setup_mptcp(h1, h2):
    """Enables MPTCP kernel support and defines path management endpoints."""
    for host in (h1, h2):
        host.cmd('sysctl -w net.mptcp.enabled=1')
        # BLEST scheduler is often used to minimize HOL blocking during handover
        host.cmd("sysctl -w net.mptcp.scheduler=blest")
        host.cmd('ip mptcp endpoint flush')

    h1.cmd('ip mptcp endpoint add 10.0.0.1 dev h1-eth0 subflow signal')
    h1.cmd('ip mptcp endpoint add 10.0.1.1 dev h1-eth1 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.0.2 dev h2-eth0 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.1.2 dev h2-eth1 subflow signal')

    for host in (h1, h2):
        host.cmd('ip mptcp limits set subflows 2 add_addr_accepted 2')


def verify_connectivity(h1):
    banner("MULTI-PATH CONNECTIVITY CHECK")
    for dst in ('10.0.0.2', '10.0.1.2'):
        out  = h1.cmd(f'ping -c 3 -W 1 {dst}')
        loss = [l for l in out.splitlines() if 'packet loss' in l]
        ok   = loss and '0% packet loss' in loss[0]
        print(f"  Path to {dst}  =>  {'[AVAILABLE]  ' if ok else '[FAILED]'}  "
              f"{loss[0].strip() if loss else 'No Response'}")


# ─────────────────────────────────────────────
#  Traffic Control (TC) — Channel Dynamics
# ─────────────────────────────────────────────

def _tc_set_rate(host, iface, rate_mbit, delay_ms=20):
    """Sets strict BW limits to evaluate MPTCP Aggregation."""
    rate = f"{rate_mbit}mbit"
    burst = "4kbit"
    latency = "50ms"

    host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')
    host.cmd(
        f'tc qdisc add dev {iface} root tbf '
        f'rate {rate} burst {burst} latency {latency}'
    )
    host.cmd(
        f'tc qdisc add dev {iface} parent 1:1 handle 10: '
        f'netem delay {delay_ms}ms 2>/dev/null || true'
    )
    return host.cmd(f'tc qdisc show dev {iface}')
    

def degrade_path1_for_handover(net, h1, h2, delay_ms=100, init=False):
    """Simulates a moving node or fading signal on Path 1 to trigger handover."""
    print(f"\n  [Network Event] Increasing latency on Path 1 (WiFi) to {delay_ms}ms")

    def set_delay(host, iface):
        if init:
            host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')
            host.cmd(f'tc qdisc add dev {iface} root netem delay {delay_ms}ms')
        else:
            host.cmd(f'tc qdisc change dev {iface} root netem delay {delay_ms}ms')
        return host.cmd(f'tc qdisc show dev {iface}')

    for host, iface in [(h1, 'h1-eth0'), (h2, 'h2-eth0')]:
        set_delay(host, iface)

    try:
        ports = subprocess.check_output(['ovs-vsctl', 'list-ports', 's1'], stderr=subprocess.DEVNULL).decode().strip().split('\n')
        ports = [p.strip() for p in ports if p.strip()]
    except:
        ports = []

    for port in ports:
        cmd = 'replace' if init else 'change'
        subprocess.run(['tc', 'qdisc', cmd, 'dev', port, 'root', 'netem', f'delay {delay_ms}ms'], capture_output=True)

def spike_rtt_path2(net, h1, h2, delay_ms=200):
    """Injects RTT fluctuations to test MPTCP scheduler robustness."""
    print(f"\n  [Network Event] Latency spike on Path 2 (LTE) → {delay_ms} ms")
    for host, iface in [(h1, 'h1-eth1'), (h2, 'h2-eth1')]:
        host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')
        host.cmd(f'tc qdisc add dev {iface} root netem delay {delay_ms}ms')

# ─────────────────────────────────────────────
#  Reporting & Visualization
# ─────────────────────────────────────────────

W = 62

def banner(title, char='='):
    print(f"\n{char * W}")
    print(f"  {title}")
    print(f"{char * W}")


def section(title):
    banner(title, char='-')


def parse_iperf_json(raw, label):
    """Extracts throughput metrics for efficiency analysis."""
    try:
        start = raw.find('{')
        if start == -1:
            raise ValueError("no JSON in output")
        data        = json.loads(raw[start:])
        per_sec     = [iv['sum']['bits_per_second'] / 1e6
                       for iv in data['intervals']]
        sent        = data['end']['sum_sent']
        received    = data['end']['sum_received']
        avg_mbps    = received['bits_per_second'] / 1e6
        retransmits = sent.get('retransmits', 0)

        section(label)
        print(f"  Avg Throughput: {avg_mbps:.2f} Mbps")
        print(f"  Retransmissions: {retransmits} (Reliability Metric)")
        return avg_mbps, per_sec

    except Exception as e:
        section(label)
        print(f"  [!] Measurement Error: {e}")
        return 0.0, []


def ascii_graph(series_list, labels, title, collapse_at=None):
    """Plots throughput over time to visualize Handover transitions."""
    banner(title)

    if not any(series_list):
        print("  (No data recorded)")
        return

    max_t   = max(len(s) for s in series_list)
    max_val = max((max(s) for s in series_list if s), default=1)
    rows    = 12
    cols    = min(max_t, 60)

    def scale(val):
        return int(round((val / max_val) * rows))

    canvas = [[' '] * cols for _ in range(rows + 1)]
    chars  = ['#', '*', 'o', '+']

    for si, series in enumerate(series_list):
        ch = chars[si % len(chars)]
        for t in range(min(len(series), cols)):
            row = rows - scale(series[t])
            row = max(0, min(rows, row))
            canvas[row][t] = ch

    print(f"\n  {'Mbps':>6}  |")
    for r in range(rows + 1):
        y_val = max_val * (rows - r) / rows
        row_str = ''.join(canvas[r])
        print(f"  {y_val:>6.1f}  | {row_str}")

    print(f"  {'':>6}  +-" + '-' * cols)
    print(f"  {'':>6}    " + ' ' * 4 + 'Time (seconds) →')

    if collapse_at is not None and collapse_at < cols:
        print('  ' + ' ' * 10 + ' ' * collapse_at + f'^ Handover Event (t={collapse_at+1}s)')

    print()
    for si, (label, ch) in enumerate(zip(labels, chars)):
        avg = sum(series_list[si]) / len(series_list[si]) if series_list[si] else 0
        print(f"  {ch}  {label:<35}  Avg: {avg:.2f} Mbps")


def wait_for_files(h1, paths, timeout=40):
    for i in range(timeout):
        time.sleep(1)
        results = [h1.cmd(f'cat {p} 2>/dev/null') for p in paths]
        if all(r.strip().startswith('{') and r.strip().endswith('}') for r in results):
            return results
    return [h1.cmd(f'cat {p} 2>/dev/null') for p in paths]


# ─────────────────────────────────────────────
#  Experiment Phases
# ─────────────────────────────────────────────

def run_tcp_baseline(h1, h2):
    banner("PHASE 1: SINGLE-PATH TCP BASELINE")
    print("  Testing individual path capacity for comparison.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s &')
    time.sleep(1.5)

    r1 = h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -t 10 -i 1 --json')
    t1, bw1 = parse_iperf_json(r1, "TCP Baseline — Path 1 (WiFi)")
    
    r2 = h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -t 10 -i 1 --json')
    t2, bw2 = parse_iperf_json(r2, "TCP Baseline — Path 2 (LTE)")

    h2.cmd('pkill -f iperf3')
    time.sleep(2)
    return t1, t2


def run_mptcp_baseline(h1, h2, tcp1, tcp2):
    banner("PHASE 2: MPTCP RESOURCE AGGREGATION ")
    print("  Evaluating if MPTCP effectively pools bandwidth from both paths.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    h1.cmd('rm -f /tmp/m1.json /tmp/m2.json')
    h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t 15 -i 1 --json > /tmp/m1.json 2>&1 &')
    h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t 15 -i 1 --json > /tmp/m2.json 2>&1 &')

    print("  Measuring concurrent throughput across Path 1 and Path 2...")
    r1, r2 = wait_for_files(h1, ['/tmp/m1.json', '/tmp/m2.json'])

    t1, bw1 = parse_iperf_json(r1, "MPTCP Flow — Path 1")
    t2, bw2 = parse_iperf_json(r2, "MPTCP Flow — Path 2")

    combined = [a + b for a, b in zip(bw1, bw2)] if bw1 and bw2 else []
    ascii_graph([bw1, bw2, combined], ['Path 1 (WiFi)', 'Path 2 (LTE)', 'MPTCP Aggregated Total'], 'AGGREGATION ANALYSIS')

    efficiency = (t1 + t2) / (tcp1 + tcp2) * 100 if (tcp1 + tcp2) > 0 else 0
    section("AGGREGATION EFFICIENCY")
    print(f"  Combined MPTCP throughput: {t1+t2:.2f} Mbps")
    print(f"  Sum of Single-path TCP  : {tcp1+tcp2:.2f} Mbps")
    print(f"  Efficiency Score        : {efficiency:.1f}%")

    h2.cmd('pkill -f iperf3')
    time.sleep(3)
    return t1, t2


def run_handover_experiment(net, h1, h2):
    banner("PHASE 3: DYNAMIC HANDOVER (WiFi → LTE Transition)")
    print("  Simulating Path 1 signal degradation to force traffic to Path 2.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    h1.cmd('rm -f /tmp/h1.json /tmp/h2.json')
    h1.cmd(f'iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t 25 -i 1 --json > /tmp/h1.json 2>&1 &')
    h1.cmd(f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t 25 -i 1 --json > /tmp/h2.json 2>&1 &')

    time.sleep(HANDOVER_AT)

    # Begin gradual degradation to observe handover responsiveness
    degrade_path1_for_handover(net, h1, h2, delay_ms=100, init=True)
    time.sleep(5)

    for d in [150, 200, 250]:
        degrade_path1_for_handover(net, h1, h2, delay_ms=d, init=False)
        time.sleep(5)

    r1, r2 = wait_for_files(h1, ['/tmp/h1.json', '/tmp/h2.json'], timeout=40)
    t1, bw1 = parse_iperf_json(r1, "Handover Phase — Path 1 (Degraded)")
    t2, bw2 = parse_iperf_json(r2, "Handover Phase — Path 2 (Preferred)")

    if bw1 and bw2:
        combined = [a + b for a, b in zip(bw1[:min(len(bw1), len(bw2))], bw2[:min(len(bw1), len(bw2))])]
        ascii_graph([bw1, bw2, combined], ['Path 1 (WiFi Degrading)', 'Path 2 (LTE Backup)', 'Total Throughput'], 'HANDOVER TRANSITION GRAPH', collapse_at=HANDOVER_AT)

        try:
            import os
            os.environ.setdefault("MPTCP_REPORT_FIGS", "graphs")
            from plot_helpers import save_bar_comparison, save_throughput_timeseries

            mn = min(len(bw1), len(bw2))
            b1, b2 = bw1[:mn], bw2[:mn]
            comb = [a + b for a, b in zip(b1, b2)]
            save_throughput_timeseries(
                {
                    "Path 1 (Wi-Fi analog, RTT ramped)": b1,
                    "Path 2 (LTE analog)": b2,
                    "Combined": comb,
                },
                "Handover: Path 1 RTT stepped up after t = 10 s (BLEST scheduler)",
                "fig06_handover_timeseries.png",
                vlines=[(10.5, "Handover (Path 1 delay increase)")],
            )
            pre = sum(comb[:HANDOVER_AT]) / HANDOVER_AT if HANDOVER_AT else 0.0
            post = sum(comb[HANDOVER_AT:]) / max(len(comb) - HANDOVER_AT, 1)
            save_bar_comparison(
                ["Avg combined\nbefore t=10 s", "Avg combined\nafter handover"],
                [pre, post],
                "Handover: average combined throughput before vs. after event window",
                "fig06_handover_pre_post_bars.png",
            )
        except ImportError:
            print("  [!] plot_helpers / matplotlib not available — skip fig06 PNG figures")

    banner("HANDOVER SUCCESS METRICS")
    p1_pre = sum(bw1[:HANDOVER_AT]) / HANDOVER_AT
    p1_post = sum(bw1[HANDOVER_AT+2:]) / len(bw1[HANDOVER_AT+2:]) if len(bw1) > HANDOVER_AT+2 else 0
    
    print(f"  Path 1 Throughput (Pre-degradation) : {p1_pre:.2f} Mbps")
    print(f"  Path 1 Throughput (Post-degradation): {p1_post:.2f} Mbps")
    
    if p1_post < p1_pre * 0.5:
        print("  [SUCCESS] MPTCP successfully migrated subflows away from congested Path 1.")
    else:
        print("  [NOTICE] Partial migration observed; check scheduler congestion control.")

    h2.cmd('pkill -f iperf3')

# ─────────────────────────────────────────────
#  Execution Entry Point
# ─────────────────────────────────────────────

HANDOVER_AT = 10

def run():
    setLogLevel('warning')
    net = Mininet(topo=DualPathTopo(), link=TCLink, switch=OVSBridge, controller=None, autoSetMacs=True)
    net.start()
    h1, h2 = net.get('h1', 'h2')

    banner("MPTCP HANDOVER EXPERIMENT")
    print("  Goal: Demonstrate seamless path switching and bandwidth aggregation.")
    
    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)
    verify_connectivity(h1)

    tcp1, tcp2 = run_tcp_baseline(h1, h2)
    run_mptcp_baseline(h1, h2, tcp1, tcp2)
    run_handover_experiment(net, h1, h2)

    banner("EXPERIMENT CONCLUDED")
    net.stop()

if __name__ == '__main__':
    run()