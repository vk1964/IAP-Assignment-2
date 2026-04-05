from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json
import subprocess

# ─────────────────────────────────────────────
#  Topology — symmetric dual path, same BW/RTT
# ─────────────────────────────────────────────

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        s1 = self.addSwitch('s1', cls=OVSBridge)   # Path 1 (stable)
        s2 = self.addSwitch('s2', cls=OVSBridge)   # Path 2 (will collapse)

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


def throttle_path2(net, h1, h2, rate_mbit=2, delay_ms=20):
    """
    Collapse Path 2 bandwidth symmetrically on ALL four half-links:

        h1 ──[h1-eth1]──> s2   (h1 egress — data packets)
        s2 ──[s2-portX]──> h2  (switch egress toward h2)
        h2 ──[h2-eth1]──> s2   (h2 egress — ACK packets back)
        s2 ──[s2-portY]──> h1  (switch egress toward h1)

    We must throttle both data AND ACK directions:
    - If only the data direction is shaped, TCP slow-start on the
      sender side still ramps up because ACKs arrive unrestricted,
      causing buffer bloat and inaccurate measurements.
    - Symmetric shaping ensures TCP congestion control on h1 sees
      the collapse as intended.

    Switch ports are identified via 'ovs-vsctl list-ports' so the
    code is robust to port numbering differences across OVS versions.
    """
    print(f"\n  [tc] Reshaping Path 2 interfaces to {rate_mbit} Mbit/s ...")

    # ── h1-eth1 and h2-eth1  (host namespaces) ──
    for host, iface in [(h1, 'h1-eth1'), (h2, 'h2-eth1')]:
        result = _tc_set_rate(host, iface, rate_mbit, delay_ms)
        status = 'OK' if 'netem' in result else 'check manually'
        print(f"  [tc] {host.name}:{iface}  => {rate_mbit} Mbit/s  [{status}]")

    # ── s2 switch ports (OVS bridge — runs in root namespace) ──
    # OVS bridges share the root network namespace, so we use
    # subprocess instead of host.cmd().
    try:
        ports_raw = subprocess.check_output(
            ['ovs-vsctl', 'list-ports', 's2'],
            stderr=subprocess.DEVNULL
        ).decode().strip().split('\n')
        ports = [p.strip() for p in ports_raw if p.strip()]
    except Exception as e:
        print(f"  [tc] Could not list s2 ports: {e} — skipping switch shaping")
        ports = []

    for port in ports:
        try:
            result = subprocess.check_output(
                ['tc', 'qdisc', 'show', 'dev', port],
                stderr=subprocess.DEVNULL
            ).decode()
            if 'netem' in result:
                ret = subprocess.run(
                    ['tc', 'qdisc', 'change', 'dev', port, 'root',
                     'netem', f'rate {rate_mbit * 1000}kbit', f'delay{delay_ms}ms'],
                    capture_output=True, text=True
                )
            else:
                # No netem qdisc yet on this switch port — add one
                subprocess.run(
                    ['tc', 'qdisc', 'add', 'dev', port, 'root',
                     'netem', f'rate {rate_mbit * 1000}kbit', f'delay{delay_ms}ms'],
                    capture_output=True, text=True
                )
            print(f"  [tc] s2:{port}  => {rate_mbit} Mbit/s  [OK]")
        except Exception as e:
            print(f"  [tc] s2:{port}  => skipped ({e})")


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
    collapse_at : second index where collapse was injected (draws marker)
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
        print('  ' + ' ' * 10 + ' ' * collapse_at + f'collapse at t={collapse_at+1}s')

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

    try:
        from plot_helpers import save_bar_comparison, save_throughput_timeseries

        save_throughput_timeseries(
            {"TCP Path 1": bw1, "TCP Path 2": bw2},
            "TCP baseline: independent single-path flows",
            "fig04_tcp_baseline_timeseries.png",
        )
        save_bar_comparison(
            ["Path 1", "Path 2", "Sum"],
            [t1, t2, t1 + t2],
            "TCP baseline: average throughput (Mbps)",
            "fig04_tcp_baseline_bars.png",
        )
    except ImportError:
        print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

    banner("TCP BASELINE SUMMARY", char='-')
    print(f"  Path 1 avg : {t1:.2f} Mbps  (cap 10 Mbps)")
    print(f"  Path 2 avg : {t2:.2f} Mbps  (cap 10 Mbps)")
    print(f"  Combined   : {t1+t2:.2f} Mbps  (theoretical max ~20 Mbps)")

    print("\n  Waiting 5s for TIME_WAIT sockets to clear...")
    time.sleep(5)
    return t1, t2


# ─────────────────────────────────────────────
#  Part 2 — MPTCP aggregation (no collapse)
# ─────────────────────────────────────────────

def run_mptcp_baseline(h1, h2, tcp1, tcp2):
    banner("PART 2 — MPTCP AGGREGATION (no failure)")
    print("  Two parallel streams, one bound per interface.")
    print("  Baseline to compare against the collapse experiment.")

    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    h1.cmd('rm -f /tmp/m1.json /tmp/m2.json')
    h1.cmd('iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 -t 15 -i 1 --json >/tmp/m1.json 2>&1 &')
    h1.cmd('iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 -t 15 -i 1 --json >/tmp/m2.json 2>&1 &')

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

    try:
        from plot_helpers import save_throughput_timeseries

        save_throughput_timeseries(
            {
                "Path 1": bw1,
                "Path 2": bw2,
                "Combined": combined,
            },
            "Dual-path transfer (pre-collapse): throughput over time",
            "fig04_mptcp_baseline_timeseries.png",
        )
    except ImportError:
        print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

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
#  Part 3 — MPTCP during bandwidth collapse
# ─────────────────────────────────────────────

COLLAPSE_AT = 10

def run_collapse_experiment(net, h1, h2):
    banner("PART 3 — BANDWIDTH COLLAPSE ON PATH 2")
    print(f"  Both paths start at 10 Mbps.")
    print(f"  At t={COLLAPSE_AT}s, Path 2 is throttled to 2 Mbps.")
    print(f"  Observing MPTCP scheduler response.")

    # ── Start servers ──
    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.3')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(1.5)

    # ── Start transfers (25s) ──
    h1.cmd('rm -f /tmp/c1.json /tmp/c2.json')
    h1.cmd(f'iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 '
           f'-t 25 -i 1 --json > /tmp/c1.json 2>&1 &')
    h1.cmd(f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 '
           f'-t 25 -i 1 --json > /tmp/c2.json 2>&1 &')

    print(f"\n  [t= 0]  Both paths running at 10 Mbps.")
    time.sleep(COLLAPSE_AT)

    print(f"  [t={COLLAPSE_AT:>2}]  Collapsing Path 2 bandwidth to 2 Mbps...")
    throttle_path2(net, h1, h2, rate_mbit=2, delay_ms=20)
    print(f"          Path 2 is now throttled to 2 Mbps.")
    print(f"          MPTCP should shift excess load to Path 1.")

    # ── Wait for transfers to finish ──
    print(f"\n  Waiting for transfers to complete (~{25 - COLLAPSE_AT}s remaining)...")
    r1, r2 = wait_for_files(h1, ['/tmp/c1.json', '/tmp/c2.json'], timeout=40)

    t1, bw1 = parse_iperf_json(r1, "Collapse experiment — Path 1(stable 10 Mbps)")
    t2, bw2 = parse_iperf_json(r2, f"Collapse experiment — Path 2(collapsed at t={COLLAPSE_AT}s)")

    combined = []
    if bw1 and bw2:
        n        = min(len(bw1), len(bw2))
        bw1      = bw1[:n]
        bw2      = bw2[:n]
        combined = [a + b for a, b in zip(bw1, bw2)]

        # ── Per-second annotated table ──
        banner("PER-SECOND THROUGHPUT — BANDWIDTH COLLAPSE")
        print(f"  {'t(s)':>5}  {'Path1':>7}  {'Path2':>7}  {'Total':>7}  "
              f"{'bar':<22}  note")
        print(f"  {'─'*5}  {'─'*7}  {'─'*7}  {'─'*7}  "
              f"{'─'*22}  {'─'*20}")

        max_c = max(combined) if combined else 1
        for i, (b1, b2, bc) in enumerate(zip(bw1, bw2, combined)):
            bar  = '█' * int((bc / max_c) * 22)
            note = ''
            if i == COLLAPSE_AT - 1:
                note = '<-- last second at 10 Mbps'
            elif i == COLLAPSE_AT:
                note = '<-- COLLAPSE INJECTED'
            elif i == COLLAPSE_AT + 1:
                note = '<-- congestion window adjusting'
            elif i == COLLAPSE_AT + 2:
                note = '<-- MPTCP re-balancing'
            print(f"  {i+1:>5}  {b1:>7.2f}  {b2:>7.2f}  {bc:>7.2f}  "
                  f"{bar:<22}  {note}")

        # ── ASCII graph ──
        ascii_graph(
            [bw1, bw2, combined],
            [f'Path 1 — stable (10 Mbps)',
             f'Path 2 — collapsed to 2 Mbps at t={COLLAPSE_AT}s',
             'Combined total'],
            'GRAPH — MPTCP during bandwidth collapse',
            collapse_at=COLLAPSE_AT
        )

        try:
            from plot_helpers import save_throughput_timeseries

            save_throughput_timeseries(
                {
                    "Path 1 (stable 10 Mbps)": bw1,
                    "Path 2 (throttled after event)": bw2,
                    "Combined": combined,
                },
                "Bandwidth collapse on Path 2: throughput over time",
                "fig04_bandwidth_collapse_timeseries.png",
                vlines=[
                    (
                        float(COLLAPSE_AT + 1),
                        "Path 2 throttled to 2 Mbps",
                    )
                ],
            )
        except ImportError:
            print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

    # ── Summary ──
    banner("COLLAPSE EXPERIMENT SUMMARY")
    if combined:
        pre  = combined[:COLLAPSE_AT]
        post = combined[COLLAPSE_AT + 2:]
        pre_avg  = sum(pre)  / len(pre)  if pre  else 0
        post_avg = sum(post) / len(post) if post else 0
        print(f"  Avg combined BEFORE collapse  : {pre_avg:.2f} Mbps")
        print(f"  Avg combined AFTER  collapse  : {post_avg:.2f} Mbps")
        if pre_avg > 0:
            print(f"  Throughput retained           :{post_avg/pre_avg*100:.1f}%")

        p1_pre  = sum(bw1[:COLLAPSE_AT]) / COLLAPSE_AT if bw1 else 0
        p1_post = sum(bw1[COLLAPSE_AT+2:]) / len(bw1[COLLAPSE_AT+2:]) \
                  if len(bw1) > COLLAPSE_AT+2 else 0
        compensated = p1_post > p1_pre * 1.1

        print(f"\n  Path 1 avg before collapse    : {p1_pre:.2f} Mbps")
        print(f"  Path 1 avg after  collapse    : {p1_post:.2f} Mbps")
        if compensated:
            print(f"  [OK]  MPTCP shifted load — Path 1 increased to compensate")
        else:
            print(f"  [INFO] Path 1 did not significantly increase")
            print(f"         (expected: MPTCP scheduler needs RTT-based trigger)")

    print(f"\n  Path 2 avg (whole 25s)        : {t2:.2f} Mbps")
    print(f"  Path 1 avg (whole 25s)        : {t1:.2f} Mbps")

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

    banner("MPTCP vs TCP — BANDWIDTH COLLAPSE EXPERIMENT")
    print("  Topology : h1 <--[s1  Path 1  10 Mbps / 40ms RTT]--> h2")
    print("             h1 <--[s2  Path 2  10 Mbps / 40ms RTT]--> h2")
    print("                         (Path 2 collapses to 2 Mbps at t=10s)")

    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)
    verify_connectivity(h1)

    tcp1, tcp2 = run_tcp_baseline(h1, h2)
    run_mptcp_baseline(h1, h2, tcp1, tcp2)
    run_collapse_experiment(net, h1, h2)

    banner("ALL EXPERIMENTS COMPLETE")
    net.stop()


if __name__ == '__main__':
    run()