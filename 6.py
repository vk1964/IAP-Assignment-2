from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json
import subprocess

# ─────────────────────────────────────────────
#  Topology — dual path, 10 Mbps each; Path 2 has higher baseline RTT
# ─────────────────────────────────────────────

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        s1 = self.addSwitch('s1', cls=OVSBridge)   # Path 1 — degrades at handover (WiFi analog)
        s2 = self.addSwitch('s2', cls=OVSBridge)   # Path 2 — higher baseline RTT (LTE analog)

        self.addLink(h1, s1, bw=10, delay='20ms')
        self.addLink(s1, h2, bw=10, delay='20ms')

        self.addLink(h1, s2, bw=10, delay='50ms')
        self.addLink(s2, h2, bw=10, delay='50ms')


# ─────────────────────────────────────────────
#  Setup helpers
# ─────────────────────────────────────────────

def setup_ips(h1, h2):
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
    Per-interface policy routing — mandatory for MPTCP subflows on eth1.
    Without separate routing tables, eth1 reply packets exit via eth0
    (the default route), the peer sees wrong-source packets, and the
    subflow is silently dropped.
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
    for host in (h1, h2):
        host.cmd('sysctl -w net.mptcp.enabled=1')
        host.cmd("sysctl -w net.mptcp.scheduler=blest")
        host.cmd('ip mptcp endpoint flush')

    h1.cmd('ip mptcp endpoint add 10.0.0.1 dev h1-eth0 subflow signal')
    h1.cmd('ip mptcp endpoint add 10.0.1.1 dev h1-eth1 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.0.2 dev h2-eth0 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.1.2 dev h2-eth1 subflow signal')

    # add_addr_accepted 2 is required — without it h2's ADD_ADDR
    # advertisements are ignored and the second subflow never opens
    for host in (h1, h2):
        host.cmd('ip mptcp limits set subflows 2 add_addr_accepted 2')


def verify_connectivity(h1):
    banner("CONNECTIVITY CHECK")
    for dst in ('10.0.0.2', '10.0.1.2'):
        out  = h1.cmd(f'ping -c 3 -W 1 {dst}')
        loss = [l for l in out.splitlines() if 'packet loss' in l]
        ok   = loss and '0% packet loss' in loss[0]
        print(f"  ping {dst}  =>  {'[OK]  ' if ok else '[FAIL]'}  "
              f"{loss[0].strip() if loss else 'no response'}")


# ─────────────────────────────────────────────
#  tc helpers — RTT shaping for Path 1 handover
# ─────────────────────────────────────────────

def degrade_path1_for_handover(net, h1, h2, delay_ms=100, init=False):
    """
    Stable handover: change RTT WITHOUT resetting qdisc
    """

    print(f"\n  [tc] Updating Path 1 → RTT {delay_ms} ms")

    def set_delay(host, iface):
        if init:
            # Only once: create qdisc
            host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')
            host.cmd(f'tc qdisc add dev {iface} root netem delay {delay_ms}ms')
        else:
            # IMPORTANT: use change, not delete+add
            host.cmd(f'tc qdisc change dev {iface} root netem delay {delay_ms}ms')

        return host.cmd(f'tc qdisc show dev {iface}')

    # Host interfaces
    for host, iface in [(h1, 'h1-eth0'), (h2, 'h2-eth0')]:
        result = set_delay(host, iface)
        print(f"  [tc] {host.name}:{iface}  => {delay_ms} ms")

    # Switch ports
    try:
        ports = subprocess.check_output(
            ['ovs-vsctl', 'list-ports', 's1'],
            stderr=subprocess.DEVNULL
        ).decode().strip().split('\n')
        ports = [p.strip() for p in ports if p.strip()]
    except:
        ports = []

    for port in ports:
        if init:
            subprocess.run(
                ['tc','qdisc','replace','dev',port,'root','netem',f'delay {delay_ms}ms'],
                capture_output=True
            )
        else:
            subprocess.run(
                ['tc','qdisc','change','dev',port,'root','netem',f'delay {delay_ms}ms'],
                capture_output=True
            )

        print(f"  [tc] s1:{port}  => {delay_ms} ms")

# ─────────────────────────────────────────────
#  Output helpers
# ─────────────────────────────────────────────

W = 62

def banner(title, char='='):
    print(f"\n{char * W}")
    print(f"  {title}")
    print(f"{char * W}")


def section(title):
    banner(title, char='-')


def parse_iperf_json(raw, label):
    """Parse iperf3 --json. Returns (avg_mbps, per_second_list)."""
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
        print(f"  Duration      : {sent['seconds']:.1f} s")
        print(f"  Avg throughput: {avg_mbps:.2f} Mbps")
        print(f"  Peak          : {max(per_sec):.2f} Mbps")
        print(f"  Min           : {min(per_sec):.2f} Mbps")
        print(f"  Data sent     : {sent['bytes'] / 1e6:.2f} MB")
        print(f"  Retransmits   : {retransmits}")
        return avg_mbps, per_sec

    except Exception as e:
        section(label)
        print(f"  [!] Parse error: {e}")
        print(f"      Raw (first 200 chars): {raw[:200]}")
        return 0.0, []


def ascii_graph(series_list, labels, title, marker_at=None, marker_label='handover'):
    """
    Draw a multi-series ASCII line graph.
    series_list : list of per-second Mbps lists
    labels      : matching list of series names
    marker_at   : 0-based second index for a vertical marker (e.g. handover start)
    marker_label: text printed under the marker (default: handover)
    """
    banner(title)

    if not any(series_list):
        print("  (no data to plot)")
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

    tick_line = '  ' + ' ' * 10
    for t in range(cols):
        tick_line += str((t + 1) % 10) if (t + 1) % 5 == 0 else ' '
    print(tick_line)
    print(f"  {'':>6}    " + ' ' * 4 + 'time (seconds) →')

    if marker_at is not None and marker_at < cols:
        marker_line = '  ' + ' ' * 10 + ' ' * marker_at + '^'
        print(marker_line)
        print('  ' + ' ' * 10 + ' ' * marker_at
              + f'{marker_label} at t={marker_at+1}s')

    print()
    for si, (label, ch) in enumerate(zip(labels, chars)):
        avg = sum(series_list[si]) / len(series_list[si]) if series_list[si] else 0
        print(f"  {ch}  {label:<35}  avg: {avg:.2f} Mbps")


def wait_for_files(h1, paths, timeout=40):
    for i in range(timeout):
        time.sleep(1)
        results = [h1.cmd(f'cat {p} 2>/dev/null') for p in paths]
        if all(r.strip().startswith('{') and r.strip().endswith('}')
               for r in results):
            print(f"  [OK] Done after ~{i+1}s")
            return results
    print(f"  [!] Timeout after {timeout}s — returning partial results")
    return [h1.cmd(f'cat {p} 2>/dev/null') for p in paths]


# ─────────────────────────────────────────────
#  Part 1 — TCP baseline
# ─────────────────────────────────────────────

def run_tcp_baseline(h1, h2):
    banner("PART 1 — TCP BASELINE (each path independently)")
    print("  Regular TCP, one path at a time, 10s each.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s &')
    time.sleep(1.5)

    r1 = h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -t 10 -i 1 --json')
    t1, bw1 = parse_iperf_json(r1, "TCP — Path 1 (10 Mbps, 20ms+20ms links)")
    time.sleep(1)

    r2 = h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -t 10 -i 1 --json')
    t2, bw2 = parse_iperf_json(r2, "TCP — Path 2 (10 Mbps, 50ms+50ms links)")

    h2.cmd('pkill -f iperf3')

    ascii_graph(
        [bw1, bw2],
        ['TCP Path 1 (10 Mbps cap)', 'TCP Path 2 (10 Mbps cap)'],
        'GRAPH — TCP baseline per path'
    )

    banner("TCP BASELINE SUMMARY", char='-')
    print(f"  Path 1 avg : {t1:.2f} Mbps  (cap 10 Mbps)")
    print(f"  Path 2 avg : {t2:.2f} Mbps  (cap 10 Mbps)")
    print(f"  Combined   : {t1+t2:.2f} Mbps  (theoretical max ~20 Mbps)")

    print("\n  Waiting 5s for TIME_WAIT sockets to clear...")
    time.sleep(5)
    return t1, t2


# ─────────────────────────────────────────────
#  Part 2 — MPTCP aggregation (before handover stress)
# ─────────────────────────────────────────────

def run_mptcp_baseline(h1, h2, tcp1, tcp2):
    banner("PART 2 — MPTCP AGGREGATION (no failure)")
    print("  Two parallel streams, one bound per interface.")
    print("  Baseline to compare against the handover run in Part 3.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    h1.cmd('rm -f /tmp/m1.json /tmp/m2.json')
    h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t 15 -i 1 --json > /tmp/m1.json 2>&1 &')
    h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t 15 -i 1 --json > /tmp/m2.json 2>&1 &')

    print("  Running 15s baseline transfer...")
    r1, r2 = wait_for_files(h1, ['/tmp/m1.json', '/tmp/m2.json'])

    t1, bw1 = parse_iperf_json(r1, "MPTCP baseline — Path 1")
    t2, bw2 = parse_iperf_json(r2, "MPTCP baseline — Path 2")

    combined = [a + b for a, b in zip(bw1, bw2)] if bw1 and bw2 else []

    ascii_graph(
        [bw1, bw2, combined],
        ['Path 1 (10 Mbps)', 'Path 2 (10 Mbps)', 'Combined total'],
        'GRAPH — MPTCP aggregation baseline'
    )

    efficiency = (t1 + t2) / (tcp1 + tcp2) * 100 if (tcp1 + tcp2) > 0 else 0

    banner("MPTCP BASELINE SUMMARY", char='-')
    print(f"  Path 1 avg         : {t1:.2f} Mbps")
    print(f"  Path 2 avg         : {t2:.2f} Mbps")
    print(f"  Combined avg       : {t1+t2:.2f} Mbps")
    print(f"  TCP combined       : {tcp1+tcp2:.2f} Mbps")
    print(f"  Aggregation eff.   : {efficiency:.1f}%")

    h2.cmd('pkill -f iperf3')
    time.sleep(3)
    return t1, t2


# ─────────────────────────────────────────────
#  Part 3 — MPTCP under Path 1 RTT degradation (handover)
# ─────────────────────────────────────────────

HANDOVER_AT = 10

def run_handover_experiment(net, h1, h2):
    banner("PART 3 — HANDOVER (Path 1 RTT increase, WiFi → LTE analogy)")

    print("  Path 1 starts with lower delay (20ms+20ms); Path 2 is slower (50ms+50ms);")
    print("  both capped at 10 Mbps. Parallel iperf clients simulate two subflows.")
    print(f"  After t={HANDOVER_AT}s wall time, Path 1 delay is stepped up on s1 and host ifaces;")
    print("  the scheduler should send more traffic on Path 2.")

    # ── Start servers ──
    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    # ── Start transfers ──
    h1.cmd('rm -f /tmp/h1.json /tmp/h2.json')
    h1.cmd(f'iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 '
           f'-t 25 -i 1 --json > /tmp/h1.json 2>&1 &')
    h1.cmd(f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 '
           f'-t 25 -i 1 --json > /tmp/h2.json 2>&1 &')

    print(f"\n  [t=0]  Transfer running; Path 1 still at baseline delay until t={HANDOVER_AT}s.")
    time.sleep(HANDOVER_AT)

    print(f"\n  [t≈{HANDOVER_AT}s]  Increasing Path 1 (s1) netem delay — handover stress...")

    # Step Path 1 RTT upward (host eth0 + switch s1 ports)
    degrade_path1_for_handover(net, h1, h2, delay_ms=100, init=True)
    time.sleep(5)

    for d in [150, 200, 250]:
        degrade_path1_for_handover(net, h1, h2, delay_ms=d, init=False)
        time.sleep(5)

    print("          Path 1 now high RTT → scheduler should prefer Path 2.")
    # ── Wait for transfers ──
    print(f"\n  Waiting for transfers (~{25 - HANDOVER_AT}s remaining)...")
    r1, r2 = wait_for_files(h1, ['/tmp/h1.json', '/tmp/h2.json'], timeout=40)

    t1, bw1 = parse_iperf_json(r1, "Handover — Path 1 (WiFi analog, RTT ramped)")
    t2, bw2 = parse_iperf_json(r2, "Handover — Path 2 (LTE analog)")

    combined = []
    if bw1 and bw2:
        n = min(len(bw1), len(bw2))
        bw1 = bw1[:n]
        bw2 = bw2[:n]
        combined = [a + b for a, b in zip(bw1, bw2)]

        # ── Per-second table (same style) ──
        banner("PER-SECOND THROUGHPUT — HANDOVER")
        print(f"  {'t(s)':>5}  {'Path1':>7}  {'Path2':>7}  {'Total':>7}  note")

        for i, (b1, b2, bc) in enumerate(zip(bw1, bw2, combined)):
            note = ''
            if i == HANDOVER_AT - 1:
                note = '<-- before Path 1 RTT increase'
            elif i == HANDOVER_AT:
                note = '<-- handover (Path 1 delay stepped up)'
            elif i == HANDOVER_AT + 1:
                note = '<-- shifting traffic'
            print(f"  {i+1:>5}  {b1:>7.2f}  {b2:>7.2f}  {bc:>7.2f}  {note}")

        # ── Graph ──
        ascii_graph(
            [bw1, bw2, combined],
            ['Path 1 — degrading (WiFi analog)',
             'Path 2 — LTE analog',
             'Combined total'],
            'GRAPH — MPTCP during handover',
            marker_at=HANDOVER_AT,
        )

    # ── Summary ──
    banner("HANDOVER SUMMARY")

    if combined:
        pre = combined[:HANDOVER_AT]
        post = combined[HANDOVER_AT+2:]

        pre_avg = sum(pre)/len(pre) if pre else 0
        post_avg = sum(post)/len(post) if post else 0

        print(f"  Avg BEFORE handover : {pre_avg:.2f} Mbps")
        print(f"  Avg AFTER  handover : {post_avg:.2f} Mbps")

        p1_pre = sum(bw1[:HANDOVER_AT]) / HANDOVER_AT
        p1_post = sum(bw1[HANDOVER_AT+2:]) / len(bw1[HANDOVER_AT+2:])

        print(f"\n  Path 1 before : {p1_pre:.2f} Mbps")
        print(f"  Path 1 after  : {p1_post:.2f} Mbps")

        if p1_post < p1_pre * 0.5:
            print("  [OK] Traffic shifted away from degraded path")
        else:
            print("  [INFO] Weak shift (scheduler dependent)")

    print(f"\n  Path 2 avg (after handover): {sum(bw2[HANDOVER_AT+2:])/len(bw2[HANDOVER_AT+2:]):.2f} Mbps")

    h2.cmd('pkill -f iperf3')

# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

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

    banner("MPTCP HANDOVER — TCP PER-PATH BASELINES + AGGREGATION + RTT SHIFT")
    print("  Topology : h1 <--[s1  Path 1  10 Mbps, 20ms+20ms per link]--> h2")
    print("             h1 <--[s2  Path 2  10 Mbps, 50ms+50ms per link]--> h2")
    print("  Part 3   : Path 1 RTT increased in steps after 10s (no bandwidth throttle on Path 2).")

    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)
    verify_connectivity(h1)

    tcp1, tcp2 = run_tcp_baseline(h1, h2)
    run_mptcp_baseline(h1, h2, tcp1, tcp2)
    run_handover_experiment(net, h1, h2)

    banner("ALL EXPERIMENTS COMPLETE")
    net.stop()


if __name__ == '__main__':
    run()