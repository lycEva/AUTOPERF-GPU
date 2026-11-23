# GPU 压测工具（NVIDIA / JMeter direct 监控）

一键压测 + 指标采集 + 曲线出图。改服务配置、施压、监控、出图、留档，一条命令跑完。

| 文件 | 作用 |
| --- | --- |
| `bin/gputest` | **命令行入口**：装到 PATH 后任意目录可执行（`run` / `config` / `monitor` / `plot` / `services` / `doctor`） |
| `install.sh` | 把 `gputest` 软链进 PATH（默认 `~/.local/bin`），`--copy` / `--uninstall` / `--update-path` |
| `run_test.sh` | 一键压测入口（bash） |
| `apply_server_config.sh` | 改 `server_config.json` → 推入容器 → 重启 → 等端口就绪（`run_test.sh` 的配置阶段） |
| `summary_monitor/collect_metrics.py` | 采样器（Python 3.7 标准库，无第三方依赖） |
| `summary_monitor/plot_metrics.py` | 绘图（只出 PNG，系统 librsvg/Cairo 光栅化） |
| `summary_monitor/run_direct.py` | 监控 + JMeter 生命周期 |

`update_server_config.sh`（被 `apply_server_config.sh` 复用）、`update_jmx_pid.py`、`gpu_monitor.sh`（旧口径）、`yc.jmx` 为兼容保留。

---

## 📥 安装

装一次就够；之后在任意目录直接 `gputest ...`。

### 1. 放好工具目录

工具可以放在任何位置（本机是 `/mnt/ssd/nlp/wdc/xncs/GPU_TOOL`）。整目录拷过去即可，不需要编译、不需要 pip：

```bash
git clone https://github.com/lycEva/AUTOPERF-GPU.git GPU_TOOL      # 或压缩包：tar xf GPU_TOOL.tar.gz
cd GPU_TOOL
chmod +x bin/gputest install.sh    # 如果拷贝丢了可执行位
```

> 装完之后别随意移动/重命名这个目录——软链指向它。真要搬，重新跑一次 `bash install.sh`，或者改用 `--copy` 装法（复制一份，和源目录脱钩）。

### 2. 确认环境依赖

| 依赖 | 自检命令 | 说明 |
| --- | --- | --- |
| Python 3.7+ | `python3 -V` | 只用标准库 |
| Docker | `docker ps` | 当前用户得在 `docker` 组（采样依赖 `docker top`） |
| NVIDIA 驱动 | `nvidia-smi` | 显存与占卡进程的唯一来源 |
| JMeter 5.4.1 | `jmeter -v` | 找不到时用 `JMETER=/path/to/jmeter` 指定 |
| librsvg / Cairo | 见下方说明 | 出图必需（唯一输出格式是 PNG） |

不用逐条对，装完跑 `gputest doctor` 会一次性检查并指出缺哪个。

出图库用工具自己的绑定方式检查（有些机器 `ldconfig -p` 列不出来）：

```bash
python3 -c "import ctypes.util as u; print(u.find_library('rsvg-2'), u.find_library('cairo'))"
# 两个名字都不为 None 就可用，例如: librsvg-2.so.2 libcairo.so.2
```

### 3. 装到 PATH

```bash
bash install.sh
```

默认软链到 `~/.local/bin`，同时提供 `gputest` 和别名 `gpu-test`：

```
🔗 /data/wdj/.local/bin/gputest -> /mnt/ssd/nlp/wdc/xncs/GPU_TOOL/bin/gputest
🔗 /data/wdj/.local/bin/gpu-test -> /mnt/ssd/nlp/wdc/xncs/GPU_TOOL/bin/gputest
```

| 装法 | 命令 | 适用 |
| --- | --- | --- |
| 默认（软链） | `bash install.sh` | 常规；工具目录更新后命令立刻是新的 |
| 指定目录 | `bash install.sh --prefix /usr/local/bin` | 想全机器可用（要有写权限） |
| 复制一份 | `bash install.sh --copy --prefix DIR` | 工具目录要删/搬，装到 `DIR/gputest.d` 后自带全部依赖 |
| 补 PATH | `bash install.sh --update-path` | 安装目录不在 PATH 上时，幂等写进 `~/.bashrc` |
| 卸载 | `bash install.sh --uninstall` | 只删装出来的命令，不动工具目录 |

### 4. 验证

```bash
exec $SHELL -l          # 或新开终端，让 PATH 生效
gputest --version       # gputest 1.0.0
gputest doctor          # 🎉 全部就绪 即安装完成
gputest services        # 列出 ports.json 里的服务名 → 端口
```

如果提示 `gputest: command not found`，说明安装目录不在当前 shell 的 PATH 上：

```bash
bash install.sh --update-path && source ~/.bashrc
```

### 不安装也能用

```bash
bash /path/to/GPU_TOOL/run_test.sh ./yc.jmx ./result <容器> 10 900 --service fs
# 或指定工具目录后直接用 CLI
GPUTEST_HOME=/path/to/GPU_TOOL /path/to/GPU_TOOL/bin/gputest doctor
```

---

## 🧭 任意目录执行（CLI）

`gputest` 是统一入口：装一次，之后在**任意目录**直接敲，不用 `cd` 到工具目录、也不用记长路径。

```bash
# 装好后（见上面「📥 安装」），在任意目录：
gputest --version
gputest doctor      # 环境自检：python3 / docker / nvidia-smi / jmeter / librsvg
gputest services    # 列出服务名 → 端口
gputest run ./yc.jmx ./result hexocr-hexocr-1 3 900 --service fs
```

| 命令 | 作用 |
| --- | --- |
| `gputest run <jmx> <结果目录> <容器> <线程数> <秒数> --service <名>` | 一键压测（= `run_test.sh`，参数完全一样） |
| `gputest config <json> <容器> <服务> <workers> <threads>` | 只改配置并等就绪（= `apply_server_config.sh`） |
| `gputest monitor --container a,b [--duration 60]` | 只采样；结束时自动出图 |
| `gputest plot <metrics.csv>` | 用已有 CSV 重新出图，默认写到 `<csv>/plots` |
| `gputest services` | 列出 `ports.json` 的服务名与端口 |
| `gputest doctor` | 环境依赖自检，缺什么一眼看到 |
| `gputest install [--prefix DIR] [--copy] [--uninstall]` | 安装 / 卸载到 PATH |

- **相对路径按当前目录解析**：`./yc.jmx`、`./result`、`./server_config.json` 指你所在目录的文件；工具自身的 `ports.json`、`summary_monitor/`、`prepare_run.py` 按工具目录定位，两边互不干扰。
- 旧写法继续可用：`bash run_test.sh ...` 与 `bash /任意/路径/run_test.sh ...` 行为和新命令一致。
- 软链安装：工具目录一更新，命令立刻是新的；只有 `--copy` 装法需要重装。
- 安装目录不在 PATH 上时，`bash install.sh --update-path` 会把它幂等地写进 `~/.bashrc`。
- 想指定工具目录：`export GPUTEST_HOME=/path/to/GPU_TOOL`。

---

## 🚀 快速开始

```bash
# 1) 只压测：按服务名取端口改写 JMX
gputest run ./yc.jmx ./result hexocr-hexocr-1 3 900 --service fs

# 2) 改配置 + 压测（一条命令）：workers=2 / threads=4 → 重启 → 等就绪 → 压 900 秒
gputest run ./yc.jmx ./result bsocr-container 10 900 --service bsocr \
  --server-config ./server_config.json --config-workers 2 --config-threads 4

# 3) 预热 120 秒 + 正式 600 秒
gputest run ./yc.jmx ./result hexocr-hexocr-1 3 600 --service fs --warmup 120

# 4) A 调用 B：压测 A，同时把 A、B 画进同一批图
gputest run ./yc.jmx ./result svc-a 10 900 --service fs --monitor-containers svc-a,svc-b
```

```bash
gputest run <jmx路径> <结果目录> <容器名> <JMeter线程数> <正式测量秒数> --service <服务名> [选项]
# 等价旧写法：bash run_test.sh <jmx路径> <结果目录> <容器名> <线程数> <秒数> --service <服务名> [选项]
```

- 没装 CLI 时把 `gputest run` 换成 `bash run_test.sh` 即可，参数一字不差。
- 前 5 个是位置参数；第 4 个是 **JMeter 线程数**，与服务配置的 workers/threads 无关，不会互相推导。
- **`--service` 必传**：服务名决定 JMX 端口（见 [ports.json](ports.json)，扁平映射、无默认端口）。查不到就报错退出，不回落。
- 原始 JMX 不改写：先在运行目录生成副本 `plan.jmx`，端口改写、预热改造、PID 映射都只作用于副本。

| 选项 | 说明 |
| --- | --- |
| `--service` | **必传**，服务名（`ports.json` 的 key） |
| `--port <端口>` | 1–65535 的整数端口号，优先级最高（只改运行副本） |
| `--ports-config <文件>` | 换一份端口配置文件（本地路径） |
| `--warmup <整数秒>` | 预热时长，非负整数，默认 0 |
| `--monitor-containers a,b` | 采哪些容器，默认等于压测容器 |
| `--monitor-interval <秒>` | 采样目标间隔，正数、可带小数，默认 0.3 |
| `--monitor-timeout <秒>` | 单轮采样 deadline，正数、可带小数，默认 2 |
| `--server-config <文件>` | 本地 `server_config.json` 路径，给了就先进配置阶段 |
| `--config-workers <整数>` / `--config-threads <整数>` | 写入服务配置的 workers / threads，非负整数 |
| `--config-target-path <容器内绝对路径>` | 目标路径，默认 `/hexapp/ai-<服务名>-serving/conf/server_config.json` |
| `--config-wait <整数秒>` | 重启后等端口就绪的上限，默认 30；`0` 表示不等 |
| `--config-settle <整数秒>` | 就绪后再稳定几秒，默认 5 |

其余环境变量：`JMETER` 指定 jmeter 路径（未设置时兜底查找常见路径）；`WARMUP` / `SERVICE` / `PORT` / `MONITOR_*` / `CONFIG_*` 可作默认值，命令行优先。

---

## ⚙️ 改配置 + 压测

同时给 `--server-config` / `--config-workers` / `--config-threads` 才启用；只给一两个直接报错，三个都不给就是纯压测。

| 步骤 | 做什么 | 失败时 |
| --- | --- | --- |
| 1 | 改本地 JSON 的 `workers`/`threads` → `docker cp` → `docker restart` | 非 0 退出，**不施压** |
| 2 | 等 `127.0.0.1:<服务端口>` 可连，最多 `--config-wait` 秒 | 超时退出，**不施压**、不生成 `plan.jmx` |
| 3 | 再稳定 `--config-settle` 秒 | — |
| 4 | 生成 `plan.jmx`、启动监控与 JMeter | 与原流程一致 |

- 端口口径与 JMX 一致：`--port` 最高优先级，否则查 `ports.json[--service]`；容器内目标目录不存在会自动创建。
- 服务起得慢就加大 `--config-wait`（如 180）。只想改配置、不压测时：

```bash
bash apply_server_config.sh ./server_config.json bsocr-container bsocr 2 4 \
  --wait 120 --target-path /opt/app/conf/server_config.json
```

改完配置会重启容器，按需执行，不要在压测进行中调用。

---

## 🖥 指标口径

| 指标 | CSV 列 | 单位 | 取数方式 |
| --- | --- | --- | --- |
| CPU 利用率 | `cpu_pct` | % | 容器主进程树（`docker top` 的 pid/ppid 展开全部后代）读 `/proc/<pid>/stat` 的 utime+stime 增量，合计可 >100% |
| 显存占用 | `vram_mib` | MiB | 占卡进程的 `nvidia-smi --query-compute-apps` 的 used_memory |
| 显存利用率 | `vram_pct` | % | 占卡进程的 `nvidia-smi pmon -c 1 -s um` 的 **`sm` 列累加**，`-` 按 0% |
| 内存 | `memory_gib` | GiB | 容器主进程树的 RSS 合计（共享页可能重复计入，不是 cgroup 内存） |

- **CPU/内存按「容器主进程树」，显存两项按「占卡进程」**（容器 PID ∩ GPU 进程 PID）。`docker exec` 出来的临时 shell 不属于服务树，不计入。
- `vram_pct` 是 SM 计算利用率，不是显存占比；显存用了多少看 `vram_mib`。多卡多进程累加可 >100%。
- 没有占卡进程时状态 `no_gpu_processes`，显存记 0，CPU/内存照常统计。
- 每轮并行跑 3 条命令（`docker top` + 两条 `nvidia-smi`），共用 `--monitor-timeout`；超时时相关字段**留空**并记 `timeout`，绝不填 0。`--monitor-interval` 是起点间隔，错过就跳，不补采。
- 状态值：`ok` / `warmup`（CPU 需基线，留空）/ `partial` / `no_gpu_processes` / `timeout` / `error`。
- 施压前先成功采样一次（写 `ready.json`），预检失败**不施压**但保留失败 CSV 与日志；监控意外退出会立即停 JMeter。

---

## 🔥 预热

`--warmup S`（非负整数，默认 0）先施压 S 秒但不记录，之后正式测量；第 5 个参数始终是**正式测量时长**。

- 预热期请求不进 JTL/聚合/HTML 报告，周期采样也不记录；正式窗口只纳入窗口内**发起**的请求（跨边界的慢请求被排除）。
- 要求模板里**恰好一个启用的普通 ThreadGroup**且有启用的采样器；控制器、串行计划等会在施压前明确拒绝。
- 运行目录多出 `measurement_window.json`；`monitor_status.json` 的 `measurement_completed=false` 一律判失败。

---

## 📂 一次跑批产出

```
results/run_<容器名>_<时间>_<随机串>/
├── plan.jmx                 # 运行副本，原始 JMX 不动
├── result.jtl / jmeter.log / report/          # 请求性能 + JMeter HTML 报告
├── metrics.csv              # 资源采样：timestamp,elapsed_s,container,cpu_pct,vram_mib,vram_pct,memory_gib,status,error,sample_duration_s
├── monitor_status.json      # samples/statuses/failed_samples/overruns/duration_s/healthy
├── ready.json               # 施压前预检
├── measurement_window.json  # 仅预热时：预热/正式窗口起止
├── server_config.json       # 仅 --server-config 时：生效配置快照
├── config_change.json       # 仅 --server-config 时：改了哪些值、就绪结果
└── plots/                   # cpu/vram/vram_utilization/memory 各一张 PNG + overview.png + summary.json
```

每次运行新建独立子目录，多次运行互不覆盖；一次命令对应一个「接口 + 压测容器」组合，多服务各用独立结果目录。

---

## 🧪 单独监控 / 多容器

```bash
# 只采样、不压测（不给 --duration 就一直采到 SIGTERM/INT，停止后自动出图）
gputest monitor --container svc-a --duration 60
# 等价：python3 summary_monitor/collect_metrics.py --container svc-a --output-dir ./monitor --interval 0.3 --timeout 2 --duration 60

# 只有 CSV 时重新出图（只产出 PNG，缺 librsvg/Cairo 会明确报错）
gputest plot ./monitor/metrics.csv
# 等价：python3 summary_monitor/plot_metrics.py ./monitor/metrics.csv --output-dir ./monitor/plots
```

- `gputest monitor` 不给 `--output-dir` 时自动建 `./monitor_<容器>_<时间>/`，采样结束自动出图。

两种多容器场景：

```bash
# 场景 1：两个无关服务分别压测 → 各用独立结果目录
gputest run ./a.jmx ./results/a svc-a 10 900 --service fs &
gputest run ./b.jmx ./results/b svc-b 10 900 --service fs &
wait

# 场景 2：A 调用 B → 一次压测，A、B 画在同一批图里（一份 CSV、两条线）
gputest run ./a.jmx ./result svc-a 10 900 --service fs --monitor-containers svc-a,svc-b
```

- 每轮 = 每个容器一条 `docker top` + **共享**两条 `nvidia-smi`，各容器数据同一时刻；峰值标注写明是哪个容器。
- 脱离压测采多容器：`--container gateway --container worker-a,worker-b`（可重复加 `--container` 合并到一份 CSV）。

---

## 📦 环境要求

先跑 `gputest doctor`，它会逐项检查下表的依赖并指出缺哪个。

| 依赖 | 说明 |
| --- | --- |
| Linux | 按 Linux 路径与 `/proc` 实现 |
| Python 3.7+ | 仅标准库 |
| Docker | 运行用户须在 `docker` 组（采样依赖 `docker top`） |
| NVIDIA 驱动 + `nvidia-smi` | 显存与进程清单的唯一来源 |
| JMeter 5.4.1 | 可用 `JMETER` 指定路径 |
| librsvg / Cairo | 出图必需（唯一输出格式是 PNG） |

---

## ✅ 回归测试

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

全部用 mock 的 docker / nvidia-smi / jmeter，不施压、不占 GPU，任意开发机可跑。
