#!/usr/bin/env python3
"""Own the direct monitor and JMeter lifecycle; fail before load on bad sampling."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from collect_metrics import positive, non_negative_int


def terminate(proc, timeout=10):
    if proc is None:
        return None
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
    return proc.returncode


def read_status(output):
    try:
        return json.loads((output / 'monitor_status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def run(container, run_dir, interval, timeout, jmeter, warmup=0):
    output = Path(run_dir).resolve()
    # run_test.sh always pre-creates the run directory; a direct invocation may not.
    output.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    received = [0]
    def on_signal(sig, _frame):
        received[0] = sig
        stop.set()
    old = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    monitor = load = None
    result = 1
    try:
        with (output / 'monitor.log').open('w', encoding='utf-8') as log:
            try:
                monitor = subprocess.Popen(
                    [sys.executable, '-u', str(Path(__file__).with_name('collect_metrics.py')),
                     '--container', container, '--output-dir', str(output),
                     '--interval', str(interval), '--timeout', str(timeout),
                     '--warmup', str(warmup)],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                ready = output / 'ready.json'
                deadline = time.monotonic() + timeout + 5
                while not stop.is_set():
                    if ready.is_file():
                        try:
                            data = json.loads(ready.read_text(encoding='utf-8'))
                        except (ValueError, OSError):
                            data = None
                        if data is not None:
                            if not data.get('ok'):
                                raise RuntimeError('monitor preflight failed: ' + data.get('error', 'unknown'))
                            break
                    if monitor.poll() is not None:
                        raise RuntimeError('monitor exited before readiness; inspect monitor.log')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('monitor readiness timed out')
                    stop.wait(0.05)
                if stop.is_set():
                    return 128 + received[0]
                if monitor.poll() is not None:
                    raise RuntimeError('monitor stopped before JMeter launch')
                if warmup:
                    print('✅ 直接监控预检通过，采样间隔={}s，超时={}s；预热 {}s 内不采样不记录'.format(
                        interval, timeout, warmup), flush=True)
                else:
                    print('✅ 直接监控预检通过，采样间隔={}s，超时={}s'.format(interval, timeout), flush=True)
                load = subprocess.Popen(jmeter, start_new_session=True)
                while load.poll() is None and not stop.is_set():
                    if monitor.poll() is not None:
                        status = read_status(output)
                        if warmup and status.get('measurement_completed'):
                            # The collector finished its formal window on its own;
                            # the load test may still be producing its report.
                            monitor = None
                            while load.poll() is None and not stop.is_set():
                                stop.wait(0.1)
                            break
                        raise RuntimeError('monitor stopped unexpectedly; stopping JMeter')
                    stop.wait(0.1)
                if stop.is_set():
                    result = 128 + received[0]
                else:
                    result = load.returncode
                    if result < 0:
                        result = 128 - result
            finally:
                if load is not None and load.poll() is None:
                    terminate(load, timeout=5)
                print('正在停止监控并生成峰值曲线…', flush=True)
                monitor_code = terminate(monitor, timeout=30)
                status = read_status(output)
                completed = status.get('measurement_completed')
                if warmup:
                    if completed and result == 0:
                        print('✅ 正式监控窗口已完成（{}s）'.format(status.get('duration_s')), flush=True)
                    elif result == 0:
                        print('❌ 预热后的正式监控窗口没有完成，本次运行不算成功：{}'.format(
                            status.get('fatal_error') or '测量窗口未建立或被中断'), file=sys.stderr, flush=True)
                        result = 2
                if result == 0 and monitor_code not in (None, 0):
                    print('❌ 请求压测结束，但监控不完整或绘图失败，请检查 monitor.log / monitor_status.json',
                          file=sys.stderr, flush=True)
                    result = 2
        if result == 0:
            print('✅ 压测与监控完成\n运行目录: {}\n请求数据: {}\n监控数据: {}\n四指标PNG图片目录: {}'.format(
                output, output / 'result.jtl', output / 'metrics.csv',
                output / 'plots'), flush=True)
        elif result not in (130, 143):
            print('❌ 本次运行未全部成功，退出码={}，请检查 {}'.format(result, output),
                  file=sys.stderr, flush=True)
        return result
    except Exception as exc:
        print('❌ {}'.format(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--interval', type=positive, default=0.3)
    parser.add_argument('--timeout', type=positive, default=2.0)
    parser.add_argument('--warmup', type=non_negative_int, default=0)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('JMeter command is required after --')
    return run(args.container, args.run_dir, args.interval, args.timeout, command, args.warmup)


if __name__ == '__main__':
    sys.exit(main())
