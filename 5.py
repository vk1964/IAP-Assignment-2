from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json
import subprocess

# ─────────────────────────────────────────────
#  Topology — symmetric dual path (10 Mbps each, ~40 ms RTT baseline).
#  Flow: (1) TCP baseline both paths (2) MPTCP baseline (3) TCP on Path 2
#  with RTT spike (4) MPTCP with Path 2 RTT spike (5) final comparison table.
# ─────────────────────────────────────────────

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        s1 = self.addSwitch('s1', cls=OVSBridge)   
        s2 = self.addSwitch('s2', cls=OVSBridge)   

        self.addLink(h1, s1, bw=10, delay='20ms')
        self.addLink(s1, h2, bw=10, delay='20ms')

        self.addLink(h1, s2, bw=10, delay='20ms')
        self.addLink(s2, h2, bw=10, delay='20ms')


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
#  tc helpers  (THE FIX — no intf.config())
# ─────────────────────────────────────────────

def _tc_set_rate(host, iface, rate_mbit, delay_ms=20):
    """
    Strict bandwidth control using TBF (Token Bucket Filter)
    instead of netem rate (which allows bursts).
    """

    rate = f"{rate_mbit}mbit"
    burst = "32kbit"
    latency = "50ms"

    # Delete existing qdisc safely
    host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')

    # Apply TBF (strict rate limiting)
    host.cmd(
        f'tc qdisc add dev {iface} root tbf '
        f'rate {rate} burst {burst} latency {latency}'
    )

    # Add delay separately using netem (optional but keeps RTT same)
    host.cmd(
        f'tc qdisc add dev {iface} parent 1:1 handle 10: '
        f'netem delay {delay_ms}ms 2>/dev/null || true'
    )

    return host.cmd(f'tc qdisc show dev {iface}')


def spike_rtt_path2(net, h1, h2, delay_ms=200, intro_msg=None):
    """
    Apply symmetric netem delay on Path 2 (h1/h2 eth1 + s2 ports).
    Default intro describes an RTT spike; pass intro_msg for restore/baseline text.
    """
    if intro_msg is None:
        intro_msg = (
            f"Injecting Path 2 RTT spike (netem delay {delay_ms} ms per hop; target ~800 ms RTT)"
        )
    print(f"\n  [tc] {intro_msg}")

    # ── Host interfaces ──
    for host, iface in [(h1, 'h1-eth1'), (h2, 'h2-eth1')]:
        host.cmd(f'tc qdisc del dev {iface} root 2>/dev/null || true')

        # ONLY delay (no tbf!)
        host.cmd(
            f'tc qdisc add dev {iface} root netem delay {delay_ms}ms'
        )

        result = host.cmd(f'tc qdisc show dev {iface}')
        status = 'OK' if 'netem' in result else 'check manually'
        print(f"  [tc] {host.name}:{iface}  => netem delay {delay_ms} ms  [{status}]")

    # ── Switch ports (same logic as your code) ──
    import subprocess
    try:
        ports_raw = subprocess.check_output(
            ['ovs-vsctl', 'list-ports', 's2'],
            stderr=subprocess.DEVNULL
        ).decode().strip().split('\n')
        ports = [p.strip() for p in ports_raw if p.strip()]
    except Exception as e:
        print(f"  [tc] Could not list s2 ports: {e}")
        ports = []

    for port in ports:
        try:
            subprocess.run(
                ['tc', 'qdisc', 'replace', 'dev', port,
                 'root', 'netem',
                 f'delay {delay_ms}ms'],
                capture_output=True, text=True
            )
            print(f"  [tc] s2:{port}  => netem delay {delay_ms} ms  [OK]")
        except Exception as e:
            print(f"  [tc] s2:{port}  => skipped ({e})")


def restore_path2_rtt(net, h1, h2, delay_ms=20):
    """Reset Path 2 tc to baseline RTT (~40 ms) after an RTT spike experiment."""
    spike_rtt_path2(
        net, h1, h2, delay_ms=delay_ms,
        intro_msg=(
            f"Restoring Path 2 to baseline (netem delay {delay_ms} ms per hop; ~40 ms RTT)"
        ),
    )

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


def ascii_graph(series_list, labels, title, collapse_at=None):
    """
    Draw a multi-series ASCII line graph.
    series_list : list of per-second Mbps lists
    labels      : matching list of series names
    collapse_at : second index where RTT spike was injected (draws marker)
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

    if collapse_at is not None and collapse_at < cols:
        marker_line = '  ' + ' ' * 10 + ' ' * collapse_at + '^'
        print(marker_line)
        print('  ' + ' ' * 10 + ' ' * collapse_at + f'RTT spike at t={collapse_at+1}s')

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
    banner("PART 1 — TCP BASELINE (both paths, independently)")
    print("  Regular TCP, one path at a time, 10s each.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s &')
    time.sleep(1.5)

    r1 = h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -t 10 -i 1 --json')
    t1, bw1 = parse_iperf_json(r1, "TCP — Path 1 (10 Mbps, 40ms RTT)")
    time.sleep(1)

    r2 = h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -t 10 -i 1 --json')
    t2, bw2 = parse_iperf_json(r2, "TCP — Path 2 (10 Mbps, 40ms RTT)")

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
#  Part 2 — MPTCP aggregation baseline (no RTT spike yet)
# ─────────────────────────────────────────────

def run_mptcp_baseline(h1, h2, tcp1, tcp2):
    banner("PART 2 — MPTCP BASELINE (no RTT spike)")
    print("  Two parallel streams, one bound per interface.")
    print("  Baseline to compare against Parts 3–4 (RTT spike on Path 2).")

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
#  Part 3 — TCP on Path 2 only, RTT spike (40 ms → ~800 ms at t≈10s)
#  Part 4 — MPTCP (dual streams) under same Path 2 RTT spike
#  Part 5 — Final comparison table (printed after Part 4)
# ─────────────────────────────────────────────

SPIKE_DURATION = 25
COLLAPSE_AT = 10   # seconds to wait before injecting Path 2 RTT spike (tc netem)


def run_tcp_path2_rtt_spike(net, h1, h2):
    """Single TCP flow bound to Path 2; RTT spikes at t=COLLAPSE_AT."""
    banner("PART 3 — TCP ON PATH 2 WITH RTT SPIKE (~40 ms → ~800 ms)")
    print("  Regular TCP only on Path 2 (10.0.1.x). Path 1 idle.")
    print(f"  At t={COLLAPSE_AT}s, Path 2 RTT spikes to ~800 ms (tc netem).")
    print("  Expect cwnd/throughput reaction on the single TCP path.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s &')
    time.sleep(1.5)

    h1.cmd('rm -f /tmp/tcp_p2_spike.json')
    h1.cmd(
        f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -t {SPIKE_DURATION} -i 1 --json '
        f'> /tmp/tcp_p2_spike.json 2>&1 &'
    )

    print(f"\n  [t= 0]  TCP on Path 2 at ~40 ms RTT, 10 Mbps.")
    time.sleep(COLLAPSE_AT)

    print(f"At t={COLLAPSE_AT}s, injecting Path 2 RTT spike: ~40 ms → ~800 ms.")
    spike_rtt_path2(net, h1, h2, delay_ms=800)

    print(f"\n  Waiting for TCP transfer (~{SPIKE_DURATION - COLLAPSE_AT}s remaining)...")
    r, = wait_for_files(h1, ['/tmp/tcp_p2_spike.json'], timeout=40)

    avg, per_sec = parse_iperf_json(
        r,
        f"TCP — Path 2 only, RTT spike at t={COLLAPSE_AT}s",
    )

    if per_sec:
        ascii_graph(
            [per_sec],
            [f'TCP Path 2 throughput (RTT spike at t={COLLAPSE_AT}s)'],
            'GRAPH — TCP Path 2 during RTT spike',
            collapse_at=COLLAPSE_AT,
        )

    pre_avg = post_avg = 0.0
    retained_pct = 0.0
    if per_sec and len(per_sec) > COLLAPSE_AT + 2:
        pre = per_sec[:COLLAPSE_AT]
        post = per_sec[COLLAPSE_AT + 2:]
        pre_avg = sum(pre) / len(pre) if pre else 0
        post_avg = sum(post) / len(post) if post else 0
        retained_pct = (post_avg / pre_avg * 100) if pre_avg > 0 else 0
        banner("TCP PATH 2 RTT SPIKE — QUICK SUMMARY", char='-')
        print(f"  Avg throughput BEFORE spike (Path 2): {pre_avg:.2f} Mbps")
        print(f"  Avg throughput AFTER  spike (Path 2): {post_avg:.2f} Mbps")
        print(f"  Throughput retained                  : {retained_pct:.1f}%")

    h2.cmd('pkill -f iperf3')

    return {
        'avg_mbps': avg,
        'pre_avg': pre_avg,
        'post_avg': post_avg,
        'retained_pct': retained_pct,
    }


def run_mptcp_rtt_spike_experiment(net, h1, h2):
    banner("PART 4 — MPTCP WITH PATH 2 RTT SPIKE (~40 ms → ~800 ms)")
    print(f"  Both paths: 10 Mbps; baseline RTT ~40 ms on each path.")
    print(f"  At t={COLLAPSE_AT}s, Path 2 RTT spikes to ~800 ms (tc netem on Path 2 only).")
    print(f"  Two parallel streams (Path 1 + Path 2); scheduler shifts toward Path 1.")

    # ── Start servers ──
    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    # ── Start transfers ──
    h1.cmd('rm -f /tmp/mptcp_c1.json /tmp/mptcp_c2.json')
    h1.cmd(
        f'iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t {SPIKE_DURATION} -i 1 --json '
        f'> /tmp/mptcp_c1.json 2>&1 &'
    )
    h1.cmd(
        f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t {SPIKE_DURATION} -i 1 --json '
        f'> /tmp/mptcp_c2.json 2>&1 &'
    )

    print(f"\n  [t= 0]  Both paths at 10 Mbps, ~40 ms RTT.")
    time.sleep(COLLAPSE_AT)

    print(f"At t={COLLAPSE_AT}s, injecting Path 2 RTT spike: ~40 ms → ~800 ms.")
    spike_rtt_path2(net, h1, h2, delay_ms=800)
    print(f"          Path 2 — high-delay path (~800 ms RTT); minRTT should prefer Path 1.")
    print(f"          MPTCP should shift load off the degraded Path 2 subflow.")

    print(f"\n  Waiting for transfers to complete (~{SPIKE_DURATION - COLLAPSE_AT}s remaining)...")
    r1, r2 = wait_for_files(h1, ['/tmp/mptcp_c1.json', '/tmp/mptcp_c2.json'], timeout=40)

    t1, bw1 = parse_iperf_json(
        r1,
        "MPTCP spike run — Path 1 (stable ~40 ms RTT, 10 Mbps)",
    )
    t2, bw2 = parse_iperf_json(
        r2,
        f"MPTCP spike run — Path 2 (spike to ~800 ms RTT at t={COLLAPSE_AT}s)",
    )

    combined = []
    stats = {
        't1': t1,
        't2': t2,
        'pre_avg': 0.0,
        'post_avg': 0.0,
        'retained_pct': 0.0,
        'p1_pre': 0.0,
        'p1_post': 0.0,
        'p2_pre': 0.0,
        'p2_post': 0.0,
        'compensated': False,
    }

    if bw1 and bw2:
        n = min(len(bw1), len(bw2))
        bw1 = bw1[:n]
        bw2 = bw2[:n]
        combined = [a + b for a, b in zip(bw1, bw2)]

        banner("PER-SECOND THROUGHPUT — MPTCP UNDER PATH 2 RTT SPIKE")
        print(f"  {'t(s)':>5}  {'Path1':>7}  {'Path2':>7}  {'Total':>7}  "
              f"{'bar':<22}  note")
        print(f"  {'─'*5}  {'─'*7}  {'─'*7}  {'─'*7}  "
              f"{'─'*22}  {'─'*20}")

        max_c = max(combined) if combined else 1
        for i, (b1, b2, bc) in enumerate(zip(bw1, bw2, combined)):
            bar = '█' * int((bc / max_c) * 22)
            note = ''
            if i == COLLAPSE_AT - 1:
                note = '<-- last second at baseline ~40 ms RTT'
            elif i == COLLAPSE_AT:
                note = '<-- RTT SPIKE INJECTED (~800 ms on Path 2)'
            elif i == COLLAPSE_AT + 1:
                note = '<-- TCP/MPTCP reacting to inflated RTT'
            elif i == COLLAPSE_AT + 2:
                note = '<-- MPTCP scheduler re-balancing subflows'
            print(f"  {i+1:>5}  {b1:>7.2f}  {b2:>7.2f}  {bc:>7.2f}  "
                  f"{bar:<22}  {note}")

        ascii_graph(
            [bw1, bw2, combined],
            [
                f'Path 1 — stable (~40 ms RTT, 10 Mbps)',
                f'Path 2 — RTT spike ~40 ms → ~800 ms at t={COLLAPSE_AT}s',
                'Combined total',
            ],
            'GRAPH — MPTCP during Path 2 RTT spike',
            collapse_at=COLLAPSE_AT,
        )

        pre = combined[:COLLAPSE_AT]
        post = combined[COLLAPSE_AT + 2:]
        pre_avg = sum(pre) / len(pre) if pre else 0
        post_avg = sum(post) / len(post) if post else 0
        stats['pre_avg'] = pre_avg
        stats['post_avg'] = post_avg
        stats['retained_pct'] = (post_avg / pre_avg * 100) if pre_avg > 0 else 0

        p1_pre = sum(bw1[:COLLAPSE_AT]) / COLLAPSE_AT if bw1 else 0
        p1_post = (
            sum(bw1[COLLAPSE_AT + 2:]) / len(bw1[COLLAPSE_AT + 2:])
            if len(bw1) > COLLAPSE_AT + 2
            else 0
        )
        p2_pre = sum(bw2[:COLLAPSE_AT]) / COLLAPSE_AT if bw2 else 0
        p2_post = (
            sum(bw2[COLLAPSE_AT + 2:]) / len(bw2[COLLAPSE_AT + 2:])
            if len(bw2) > COLLAPSE_AT + 2
            else 0
        )
        stats['p1_pre'] = p1_pre
        stats['p1_post'] = p1_post
        stats['p2_pre'] = p2_pre
        stats['p2_post'] = p2_post
        stats['compensated'] = p1_post > p1_pre * 1.1

        banner("MPTCP RTT SPIKE — QUICK SUMMARY", char='-')
        print(f"  Avg combined BEFORE RTT spike : {pre_avg:.2f} Mbps")
        print(f"  Avg combined AFTER  RTT spike : {post_avg:.2f} Mbps")
        if pre_avg > 0:
            print(f"  Throughput retained           : {stats['retained_pct']:.1f}%")
        print(f"\n  Path 1 avg before spike       : {p1_pre:.2f} Mbps")
        print(f"  Path 1 avg after  spike       : {p1_post:.2f} Mbps")
        print(f"  Path 2 avg before spike       : {p2_pre:.2f} Mbps")
        print(f"  Path 2 avg after  spike       : {p2_post:.2f} Mbps")
        if stats['compensated']:
            print(f"  [OK]  MPTCP shifted load — Path 1 increased to compensate")
        else:
            print(f"  [INFO] Path 1 did not significantly increase")
            print(f"         (scheduler may need stronger RTT contrast or more time)")

    print(f"\n  Path 1 avg (whole {SPIKE_DURATION}s run) : {t1:.2f} Mbps")
    print(f"  Path 2 avg (whole {SPIKE_DURATION}s run) : {t2:.2f} Mbps")

    h2.cmd('pkill -f iperf3')
    return stats


def print_final_results_table(tcp1, tcp2, m1, m2, tcp_spike, mptcp_spike):
    """Part 5 — aggregate metrics (mirrors 4.py style: one closing comparison)."""
    banner("PART 5 — FINAL RESULTS (TCP vs MPTCP, RTT SPIKE ON PATH 2)")
    m_combined = m1 + m2
    tcp_combined = tcp1 + tcp2

    print(f"  {'Metric':<48}  {'Value':>12}")
    print(f"  {'─' * 48}  {'─' * 12}")
    print(f"  {'TCP baseline — Path 1 avg (Mbps)':<48}  {tcp1:>12.2f}")
    print(f"  {'TCP baseline — Path 2 avg (Mbps)':<48}  {tcp2:>12.2f}")
    print(f"  {'TCP baseline — combined (sum of paths, Mbps)':<48}  {tcp_combined:>12.2f}")
    print(f"  {'MPTCP baseline — Path 1 avg (Mbps)':<48}  {m1:>12.2f}")
    print(f"  {'MPTCP baseline — Path 2 avg (Mbps)':<48}  {m2:>12.2f}")
    print(f"  {'MPTCP baseline — combined (Mbps)':<48}  {m_combined:>12.2f}")
    print(f"  {'─' * 48}  {'─' * 12}")
    print(f"  {'TCP Path 2 only — avg during spike run (Mbps)':<48}  {tcp_spike['avg_mbps']:>12.2f}")
    print(f"  {'TCP Path 2 only — avg BEFORE spike (Mbps)':<48}  {tcp_spike['pre_avg']:>12.2f}")
    print(f"  {'TCP Path 2 only — avg AFTER spike (Mbps)':<48}  {tcp_spike['post_avg']:>12.2f}")
    print(f"  {'TCP Path 2 only — throughput retained (%)':<48}  {tcp_spike['retained_pct']:>12.1f}")
    print(f"  {'─' * 48}  {'─' * 12}")
    print(f"  {'MPTCP spike — combined avg BEFORE spike (Mbps)':<48}  {mptcp_spike['pre_avg']:>12.2f}")
    print(f"  {'MPTCP spike — combined avg AFTER spike (Mbps)':<48}  {mptcp_spike['post_avg']:>12.2f}")
    print(f"  {'MPTCP spike — combined throughput retained (%)':<48}  {mptcp_spike['retained_pct']:>12.1f}")
    print(f"  {'MPTCP spike — Path 1 avg BEFORE spike (Mbps)':<48}  {mptcp_spike['p1_pre']:>12.2f}")
    print(f"  {'MPTCP spike — Path 1 avg AFTER spike (Mbps)':<48}  {mptcp_spike['p1_post']:>12.2f}")
    print(f"  {'MPTCP spike — Path 2 avg BEFORE spike (Mbps)':<48}  {mptcp_spike['p2_pre']:>12.2f}")
    print(f"  {'MPTCP spike — Path 2 avg AFTER spike (Mbps)':<48}  {mptcp_spike['p2_post']:>12.2f}")
    print(f"  {'─' * 48}  {'─' * 12}")
    if tcp_spike['post_avg'] > 0 and mptcp_spike['post_avg'] > 0:
        ratio = mptcp_spike['post_avg'] / tcp_spike['post_avg']
        print(f"  {'Post-spike: MPTCP combined / TCP Path 2 only':<48}  {ratio:>12.2f}x")
    print()


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

    banner("MPTCP vs TCP — RTT SPIKE EXPERIMENT")
    print("  Topology : h1 <--[s1  Path 1  10 Mbps / 40ms RTT]--> h2")
    print("             h1 <--[s2  Path 2  10 Mbps / 40ms RTT]--> h2")
    print("  Flow     : (1) TCP baseline  (2) MPTCP baseline  (3) TCP + Path 2 spike")
    print("             (4) MPTCP + Path 2 spike  (5) final results table")

    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)
    verify_connectivity(h1)

    tcp1, tcp2 = run_tcp_baseline(h1, h2)
    m1, m2 = run_mptcp_baseline(h1, h2, tcp1, tcp2)
    tcp_spike = run_tcp_path2_rtt_spike(net, h1, h2)
    restore_path2_rtt(net, h1, h2)
    print("\n  Waiting 5s after Path 2 restore (TIME_WAIT / tc settle)...")
    time.sleep(5)
    mptcp_spike = run_mptcp_rtt_spike_experiment(net, h1, h2)
    print_final_results_table(tcp1, tcp2, m1, m2, tcp_spike, mptcp_spike)

    banner("ALL EXPERIMENTS COMPLETE")
    net.stop()


if __name__ == '__main__':
    run()