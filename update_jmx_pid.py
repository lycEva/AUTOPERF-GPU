import xml.etree.ElementTree as ET
import re
import subprocess
import argparse


# ===== 提取旧PID =====
def extract_old_pids(root):
    pids = set()
    for prop in root.iter("stringProp"):
        text = prop.text or ""
        pids.update(re.findall(r'pid=(\d+)', text))
        pids.update(re.findall(r'grep\s+(\d+)', text))
    return sorted(pids)


# ===== 容器进程树辅助 =====
# 不再是所有服务都用 gunicorn：ocr-serving / uvicorn / 自研二进制都见过，
# 所以这里只按「进程树」取业务进程，不绑定进程名。
HELPER_CMDS = ("bash", "sh", "tini", "tail", "sleep", "grep", "cat", "pause", "docker-init")


def _is_helper(cmd):
    """入口脚本、常驻 shell、sleep/tini 之类的辅助进程，不算业务 worker。"""
    base = cmd.strip().split()[0] if cmd.strip() else ""
    name = base.rsplit("/", 1)[-1]
    return name in HELPER_CMDS


def get_processes(container):
    """返回容器内进程列表 [(pid, ppid, cmd)]，拿不到就返回 []。"""
    cmd = f"docker top {container} -eo pid,ppid,cmd"

    try:
        output = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        return []

    processes = []
    for line in output.strip().splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            processes.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return processes


# ===== 获取容器 worker PID =====
def get_worker_pids(container):
    processes = get_processes(container)
    if not processes:
        return []

    # 优先只看业务进程（排除 bash/tini/sleep 等），排完为空才退回全量
    serving = [p for p in processes if not _is_helper(p[2])]
    pool = serving or processes

    pid_set = set(pid for pid, _, _ in pool)

    # 找 master：它的 ppid 不在 pool 的 pid 集合里
    master_pid = None
    for pid, ppid, _ in pool:
        if ppid not in pid_set:
            master_pid = pid
            break

    if master_pid:
        # 找 worker：ppid == master_pid
        worker_pids = [pid for pid, ppid, _ in pool if ppid == master_pid]
        if worker_pids:
            return sorted(worker_pids)

    # 没有清晰的 master/worker 结构（例如 ocr-serving 多进程平铺）：
    # 直接返回 pool 里的全部 pid，由调用方判断够不够。
    return sorted(p for p, _, _ in pool)


# ===== 替换 PID =====
def replace_pid(text, pid_map):
    if not text:
        return text

    for old, new in pid_map.items():
        text = re.sub(rf'pid={old}\b', f'pid={new}', text)
        text = re.sub(rf'grep\s+{old}\b', f'grep {new}', text)

    return text


# ===== 修改线程数 =====
def update_threads(root, threads):
    for prop in root.iter("stringProp"):
        if prop.attrib.get("name") == "ThreadGroup.num_threads":
            print(f"线程数: {prop.text} → {threads}")
            prop.text = str(threads)


# ===== 修改压测时间 =====
def update_duration(root, duration):
    for prop in root.iter("stringProp"):
        if prop.attrib.get("name") == "ThreadGroup.duration":
            print(f"压测时间: {prop.text} → {duration}")
            prop.text = str(duration)


# ===== 更新JMX =====
def update_jmx(file_path, container, threads, duration):
    tree = ET.parse(file_path)
    root = tree.getroot()

    old_pids = extract_old_pids(root)

    if old_pids:
        new_pids = get_worker_pids(container)

        if len(new_pids) < len(old_pids):
            # GPU_TOOL 已移除 ServerAgent/PerfMon，JMX 里的 pid= / grep <pid> 只是历史遗留。
            # PID 数量不足不再中断压测：只告警并跳过替换，监控侧（summary_monitor）会自行解析 PID。
            print(f"⚠️ 容器 {container} 只解析到 {len(new_pids)} 个业务 PID"
                  f"（JMX 里需要 {len(old_pids)} 个），跳过 PID 替换，继续压测")
            pid_map = {}
        else:
            pid_map = dict(zip(old_pids, new_pids))

            print("🔗 PID映射:")
            for k, v in pid_map.items():
                print(f"{k} → {v}")
    else:
        # GPU_TOOL 已去掉 ServerAgent/PerfMon，JMX 中不再带 pid= 引用，
        # 进程 PID 由 summary_monitor/collect_metrics.py 在采样时自行解析
        pid_map = {}
        print("ℹ️ JMX 中没有 pid= / grep <pid> 引用，跳过 PID 替换（由 summary_monitor 采样时解析）")

    # 替换 PID
    for prop in root.iter("stringProp"):
        old = prop.text
        new = replace_pid(old, pid_map)
        if old != new:
            prop.text = new

    # 修改线程数 & 时间
    update_threads(root, threads)
    update_duration(root, duration)

    tree.write(file_path, encoding="utf-8", xml_declaration=True)
    print("✅ JMX更新完成")


# ===== 主函数 =====
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("jmx")
    parser.add_argument("container")
    parser.add_argument("threads", type=int)
    parser.add_argument("duration", type=int)

    args = parser.parse_args()

    update_jmx(args.jmx, args.container, args.threads, args.duration)


if __name__ == "__main__":
    main()