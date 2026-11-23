#!/usr/bin/env python3
"""Bounded, non-overlapping resource sampling for NVIDIA GPUs (nvidia-smi).

One CSV row per container per sample with the four metrics CPU / VRAM / VRAM
utilization / memory. Several containers can be monitored in one run: the GPU
probes are shared, so every container's numbers come from the same instant.
Statistics are gathered from two different process sets:

  * CPU          -> the container's main process tree (init + all descendants),
                    so masters, entrypoints and sidecars that burn CPU count.
  * VRAM, VRAM %, memory -> only the container processes that actually hold
                    GPU memory (`docker top` PID ∩ nvidia-smi PID).

GPU environment differences (ROCm -> NVIDIA):
  * process list:  rocm-smi --showpids
                   -> nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory
  * VRAM MiB:      "VRAM USED(MiB)" per process
                   -> the used_memory column (already MiB)
  * VRAM %:        "VRAM USED(%)" per process
                   -> nvidia-smi pmon -c 1 -s um, the `sm` (SM utilization)
                      column summed over the matched processes; an idle
                      process reports `-`, which is a real 0 %.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

FIELDS = ('timestamp', 'elapsed_s', 'container', 'cpu_pct', 'vram_mib',
          'vram_pct', 'memory_gib', 'status', 'error', 'sample_duration_s')

# nvidia-smi, unlike rocm-smi, answers a query with no rows using this wording.
NO_PROCESSES = re.compile(r'no\s+running\s+(?:compute\s+)?processes', re.I)
GPU_UUID = re.compile(r'^GPU-[0-9A-Za-z-]+$')


class Stopped(Exception):
    pass


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite number greater than zero')
    return number


def non_negative_int(value):
    text = str(value)
    if not re.fullmatch(r'0|[1-9][0-9]*', text):
        raise argparse.ArgumentTypeError('warmup must be a non-negative integer number of seconds')
    return int(text)


def kill_group(proc):
    # Even if the leader exited, descendants can still hold pipes or run commands.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
        # A killed process that is slow to be reaped must not surface as a
        # bogus sampling error for this round.
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def commands(containers, timeout, stop):
    """One docker top per container plus the two GPU probes, all in parallel.

    Container PIDs and the GPU process list must come from the same instant, so
    every command shares one deadline; a container that hangs fails the round
    instead of silently producing a per-container bias.
    """
    deadline = time.monotonic() + timeout
    processes = []
    outputs = []
    argvs = [['docker', 'top', name, '-eo', 'pid,ppid,rss'] for name in containers]
    argvs.append(['nvidia-smi', '--query-compute-apps=pid,gpu_uuid,used_memory',
                  '--format=csv,noheader,nounits'])
    argvs.append(['nvidia-smi', 'pmon', '-c', '1', '-s', 'um'])
    try:
        for argv in argvs:
            if stop.is_set():
                raise Stopped()
            processes.append(subprocess.Popen(argv, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True,
                                              start_new_session=True))
        for proc in processes:
            while True:
                if stop.is_set():
                    raise Stopped()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('sample exceeded {:.3f}s (docker/nvidia-smi)'.format(timeout))
                try:
                    out, err = proc.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if proc.returncode:
                raise RuntimeError('{} exit {}: {}'.format(proc.args[0], proc.returncode,
                                                           err.strip()[:400]))
            outputs.append(out)
        return outputs
    finally:
        for proc in processes:
            kill_group(proc)


def parse_top(text):
    """docker top -eo pid,ppid,rss -> {pid: (ppid, rss_kib)}."""
    pids = {}
    lines = text.splitlines()
    if not lines or not all(name in lines[0] for name in ('PID', 'PPID', 'RSS')):
        raise ValueError('unrecognized docker top PID/PPID/RSS output')
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
            pids[int(parts[0])] = (int(parts[1]), int(parts[2]))
        elif line.strip():
            raise ValueError('invalid docker top row: ' + line[:120])
    if not pids:
        raise ValueError('container has no visible processes')
    return pids


def process_tree(pids, anchors=()):
    """Main process tree of the service: the tree the GPU holders live in.

    `docker top` shows host PIDs, so a process started with `docker exec` has a
    parent outside the container's set and would look like a second root. That
    stray shell is not the service, so the tree is anchored on the GPU-holding
    processes and expanded upwards to their topmost listed ancestor, then back
    down over every descendant. Without any GPU holder, every root is used.
    """
    parent = {pid: ppid for pid, (ppid, _) in pids.items()}

    def root_of(pid):
        seen = set()
        while parent.get(pid) in parent and pid not in seen:
            seen.add(pid)
            pid = parent[pid]
        return pid

    roots = {root_of(pid) for pid in anchors if pid in parent}
    if not roots:
        roots = {pid for pid, ppid in parent.items() if ppid not in parent}
    children = {}
    for pid, ppid in parent.items():
        children.setdefault(ppid, []).append(pid)
    tree = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        tree.add(pid)
        stack.extend(children.get(pid, ()))
    return tree


def parse_apps(text):
    """nvidia-smi --query-compute-apps -> {pid: [(uuid, used_mib), ...]}."""
    processes = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if NO_PROCESSES.search(line):
            continue
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 3 or not parts[0].isdigit() or not GPU_UUID.match(parts[1]):
            raise ValueError('unrecognized nvidia-smi compute-apps row: ' + line[:120])
        try:
            used = float(parts[2])
        except ValueError:
            raise ValueError('invalid used_memory for PID {}: {}'.format(parts[0], parts[2][:40]))
        if not math.isfinite(used) or used < 0:
            raise ValueError('invalid used_memory for PID {}: {}'.format(parts[0], parts[2][:40]))
        processes.setdefault(int(parts[0]), []).append((parts[1], used))
    return processes


def parse_pmon(text):
    """nvidia-smi pmon -c 1 -s um -> {pid: sm_percent}.

    pmon prints one row per (GPU, process): gpu idx, pid, type, sm, mem, enc,
    dec, jpg, ofa, fb, ccpm, command. `sm` is the SM (compute) utilization of
    that process during the sample; an idle process reports `-`, which is a
    genuine zero, not missing data.
    """
    sm = {}
    rows = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parts = stripped.split()
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError('unrecognized nvidia-smi pmon row: ' + line[:120])
        pid = int(parts[1])
        field = parts[3]
        if field == '-':
            value = 0.0
        else:
            try:
                value = float(field)
            except ValueError:
                raise ValueError('invalid pmon sm value for PID {}: {}'.format(pid, field[:20]))
            if not math.isfinite(value) or value < 0:
                raise ValueError('invalid pmon sm value for PID {}: {}'.format(pid, field[:20]))
        sm[pid] = sm.get(pid, 0.0) + value
        rows += 1
    if not rows and text.strip() and 'pid' not in text.split('pid', 1)[0][:200]:
        raise ValueError('unrecognized nvidia-smi pmon output (not a zero reading)')
    return sm


class Sampler:
    def __init__(self, containers, timeout, stop, proc_root=None):
        if isinstance(containers, str):
            containers = [containers]
        self.containers = list(containers)
        self.timeout = timeout
        self.stop = stop
        self.proc_root = Path(proc_root or os.environ.get('PROC_ROOT', '/proc'))
        self.hz = os.sysconf('SC_CLK_TCK')
        self.previous = {}

    def sample(self):
        """One round for every container; the GPU probes are shared."""
        outputs = commands(self.containers, self.timeout, self.stop)
        count = len(self.containers)
        tops = [parse_top(text) for text in outputs[:count]]
        apps = parse_apps(outputs[count])
        sm = parse_pmon(outputs[count + 1])
        current = {}
        results = []
        for container, top in zip(self.containers, tops):
            matched = sorted(top.keys() & apps.keys())
            tree = process_tree(top, matched)
            values = {'vram_mib': sum(used for pid in matched for _, used in apps[pid]),
                      # 显存利用率 = nvidia-smi pmon 的 sm%（进程计算利用率）累加,
                      # 不是 used/total 的显存占比。多卡多进程合计可超过 100%。
                      'vram_pct': sum(sm.get(pid, 0.0) for pid in matched),
                      # 内存与 CPU 同口径：容器主进程树的 RSS 合计（共享页可能重复
                      # 计入，不是 cgroup 内存）。
                      'memory_gib': sum(top[p][1] for p in tree) / 1048576.0}
            cpu = 0.0
            warmup = False
            failures = []
            # CPU is the whole service process tree, not only the GPU holders: a
            # master/entrypoint/sidecar burns CPU too and must count in both cases.
            for pid in sorted(tree):
                try:
                    # comm can contain spaces or parentheses. Fields after its final
                    # closing parenthesis start with state; utime/stime are 12/13.
                    stat = (self.proc_root / str(pid) / 'stat').read_text()
                    fields = stat[stat.rfind(')') + 2:].split()
                    ticks, start = int(fields[11]) + int(fields[12]), fields[19]
                    now = time.monotonic()
                    current[(container, pid)] = (ticks, start, now)
                    old = self.previous.get((container, pid))
                    if old and start == old[1] and now > old[2] and ticks >= old[0]:
                        cpu += (ticks - old[0]) * 100.0 / self.hz / (now - old[2])
                    else:
                        warmup = True
                except (OSError, ValueError, IndexError) as exc:
                    failures.append('{} PID {} CPU unavailable: {}'.format(container, pid, exc))
            values['cpu_pct'] = None if warmup or failures else cpu
            # "No GPU holder" is the more important signal than a missing CPU
            # baseline, so it wins; CPU stays empty until the baseline exists.
            if failures:
                status = 'partial'
            elif not matched:
                status = 'no_gpu_processes'
            elif warmup:
                status = 'warmup'
            else:
                status = 'ok'
            results.append({'container': container, 'values': values,
                            'status': status, 'error': '; '.join(failures)})
        self.previous = current
        return results


WINDOW_FIELDS = ('load_started_epoch_s', 'measurement_start_epoch_s',
                 'measurement_end_epoch_s', 'warmup_s', 'duration_s')


def read_window(path):
    """Read and validate the measurement window JMeter publishes atomically."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('unreadable measurement window: {}'.format(exc))
    if not isinstance(data, dict):
        raise ValueError('measurement window is not an object')
    values = {}
    for name in WINDOW_FIELDS:
        try:
            values[name] = float(data[name])
        except (KeyError, TypeError, ValueError):
            raise ValueError('measurement window is missing {}'.format(name))
        if not math.isfinite(values[name]):
            raise ValueError('measurement window {} is not finite'.format(name))
    if values['duration_s'] <= 0:
        raise ValueError('measurement window duration must be greater than zero')
    if values['measurement_end_epoch_s'] <= values['measurement_start_epoch_s']:
        raise ValueError('measurement window end must be after its start')
    return values


def wait_window(path, stop, timeout=None):
    """Wait for the window file; a partial write is impossible by construction."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while not stop.is_set():
        if Path(path).is_file():
            return read_window(path)
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError('measurement window was never established')
        stop.wait(0.05)
    return None


def collect(containers, output_dir, interval=0.3, timeout=2.0, duration=None, warmup=0):
    """Monitor one container, or several at once with shared GPU probes."""
    if isinstance(containers, str):
        containers = [containers]
    containers = [name.strip() for name in containers if str(name).strip()]
    if not containers:
        raise ValueError('at least one Docker container name is required')
    for name in containers:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('invalid Docker container name: ' + name)
    containers = list(dict.fromkeys(containers))
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError('warmup must be a non-negative integer number of seconds')
    for value in (interval, timeout):
        positive(value)
    if duration is not None:
        positive(duration)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / 'metrics.csv'
    if csv_path.exists() or (output / 'ready.json').exists():
        raise ValueError('monitor output already exists; use a new directory')
    stop = threading.Event()
    previous_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    sampler = Sampler(containers, timeout, stop)
    started = time.monotonic()
    counts = {}
    failures = overruns = samples = 0
    first = True
    fatal = ''
    window = None
    declared = None
    completed = False
    try:
        with csv_path.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            stream.flush()

            def record(begin, elapsed, results):
                """Write one row per container; a failure never becomes a zero."""
                nonlocal samples, failures
                duration = round(time.monotonic() - begin, 6)
                stamp = dt.datetime.now().astimezone().isoformat(timespec='milliseconds')
                rows = []
                for item in results:
                    row = dict.fromkeys(FIELDS, '')
                    row.update(timestamp=stamp, elapsed_s=round(elapsed, 6),
                               container=item['container'])
                    values = item['values']
                    if values is not None:
                        row.update({key: ('' if value is None else round(value, 6))
                                    for key, value in values.items()})
                    row.update(status=item['status'], error=item['error'])
                    row['sample_duration_s'] = duration
                    samples += 1
                    status = item['status']
                    counts[status] = counts.get(status, 0) + 1
                    if status in ('timeout', 'error', 'partial'):
                        failures += 1
                        # The full errors remain in CSV; rate-limit log volume.
                        if failures <= 5 or failures % 100 == 0:
                            print('WARNING sample {}: {}'.format(samples, row['error']),
                                  file=sys.stderr, flush=True)
                    writer.writerow(row)
                    rows.append(row)
                stream.flush()
                return rows

            def measure(begin):
                try:
                    return sampler.sample()
                except Stopped:
                    raise
                except Exception as exc:
                    # A gap must never be interpreted as a zero CPU reading.
                    sampler.previous = {}
                    return [{'container': name, 'values': None,
                             'status': 'timeout' if isinstance(exc, TimeoutError) else 'error',
                             'error': '{}: {}'.format(type(exc).__name__, exc)}
                            for name in containers]

            def publish_ready(results):
                bad = [item for item in results
                       if item['status'] in ('timeout', 'error', 'partial')]
                ready = not bad
                (output / 'ready.json').write_text(json.dumps(
                    {'ok': ready,
                     'error': '; '.join(item['error'] for item in results if item['error']),
                     'status': 'ok' if ready else bad[0]['status'],
                     'containers': containers, 'pid': os.getpid()},
                    ensure_ascii=False), encoding='utf-8')
                return ready

            if warmup:
                # Warmup: one unrecorded preflight sample proves the tools work,
                # then nothing is sampled or recorded until the load test itself
                # opens the formal measurement window.
                begin = time.monotonic()
                try:
                    results = measure(begin)
                except Stopped:
                    results = None
                if results is not None:
                    first = False
                    if not publish_ready(results):
                        fatal = 'initial sample failed; do not start load test'
                if not fatal and not stop.is_set():
                    # The formal window starts from a fresh CPU baseline.
                    sampler.previous = {}
                    try:
                        window = wait_window(output / 'measurement_window.json', stop)
                    except ValueError as exc:
                        fatal = str(exc)
                        window = None
                    if window is not None:
                        declared = window['duration_s']
                        started = window['measurement_start_epoch_s']
                        end = window['measurement_end_epoch_s']
                        while not stop.is_set():
                            remaining = started - time.time()
                            if remaining > 0:
                                stop.wait(min(0.1, remaining))
                                continue
                            if time.time() >= end:
                                completed = True
                                break
                            begin = time.monotonic()
                            try:
                                results = measure(begin)
                            except Stopped:
                                break
                            record(begin, time.time() - started, results)
                            # Start-to-start schedule inside the window: the sample
                            # cost is subtracted, and nothing runs past the end.
                            delay = min(max(0.0, begin + interval - time.monotonic()),
                                        max(0.0, end - time.time()))
                            stop.wait(delay)
            while not warmup and not stop.is_set():
                begin = time.monotonic()
                if duration is not None and begin - started >= duration and not first:
                    break
                try:
                    results = measure(begin)
                except Stopped:
                    break
                record(begin, begin - started, results)
                if first:
                    first = False
                    if not publish_ready(results):
                        fatal = 'initial sample failed; do not start load test'
                        break
                # Monotonic start-to-start schedule, skip missed slots; do not
                # accumulate an unbounded queue of parallel samples.
                target = begin + interval
                now = time.monotonic()
                if now > target:
                    overruns += 1
                    target += (math.floor((now - target) / interval) + 1) * interval
                delay = target - now
                if duration is not None:
                    delay = min(delay, max(0.0, started + duration - now))
                stop.wait(max(0.0, delay))
    finally:
        metadata = {'containers': containers,
                    'container': containers[0] if len(containers) == 1 else containers,
                    'interval_s': interval, 'timeout_s': timeout,
                    'samples': samples, 'statuses': counts, 'failed_samples': failures,
                    'overruns': overruns, 'duration_s': declared if (warmup and declared) else
                    time.monotonic() - started,
                    'fatal_error': fatal, 'healthy': samples > 0 and failures == 0}
        if warmup:
            # The preflight sample is deliberately not part of the measurement,
            # and a run that never completed its window is not a success.
            metadata.update(warmup_s=warmup, preflight_recorded=False,
                            measurement_completed=bool(completed),
                            measurement_window=window)
        (output / 'monitor_status.json').write_text(json.dumps(metadata, indent=2,
                                                               ensure_ascii=False), encoding='utf-8')
        try:
            from plot_metrics import render
            render(csv_path, output / 'plots')
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    if warmup:
        return 0 if (completed and samples and not failures) else 2
    return 2 if failures or not samples else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', action='append', required=True,
                        metavar='NAME[,NAME...]',
                        help='container to monitor; repeat the flag or pass a comma-separated list')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--interval', type=positive, default=0.3)
    parser.add_argument('--timeout', type=positive, default=2.0)
    parser.add_argument('--duration', type=positive)
    parser.add_argument('--warmup', type=non_negative_int, default=0,
                        help='warmup seconds: sample once for preflight, then record only '
                             'the measurement window JMeter publishes')
    args = parser.parse_args()
    names = [name for value in args.container for name in value.split(',')]
    try:
        return collect(names, args.output_dir, args.interval, args.timeout,
                       args.duration, args.warmup)
    except Exception as exc:
        print('ERROR monitoring: {}: {}'.format(type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
