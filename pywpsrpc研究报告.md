# pywpsrpc 项目研究 + WPS Linux docx→pdf 转换研究报告

> 研究时间：2026 年 8 月 | 目标：用通用 WPS Linux + pywpsrpc RPC 完成 docx→pdf 转换
> 结论：**pywpsrpc RPC 方案已完整跑通**——主镜像 WPS 11.1.0.9662 + pywpsrpc v1.1.0；**备用镜像 WPS 12.1.2.28080 + pywpsrpc 2.4.0 亦实测可用**（三组件转 PDF 通过），详见文末"重大更新"与"WPS 12 验证"章节。

---

## 一、pywpsrpc 项目核心信息

- **仓库**：`timxx/pywpsrpc`（GitHub），MIT 协议
- **当前版本**：2.4.0（PyPI），含 `rpcwpsapi`（文字）、`rpcwppapi`（演示）、`rpcetapi`（表格）三模块
- **依赖**：`librpcwpsapi_wpsqt.so`（客户端库）+ Qt5 运行环境
- **核心 API**：`createWpsRpcInstance()` → `rpc.getWpsApplication()` → 文档对象 → `SaveAs2(path, FileFormat=wdFormatPDF)`

## 二、Issues 关键发现（共 120+ issues）

| 类别 | 代表 issue | 关键结论 |
|---|---|---|
| 环境安装 | #92、#111 | 只有 WPS 11.x 带 `librpcwpsapi_sysqt5.so`；Docker 需 Qt5 全套 + libtiff.so.5 链接 |
| **运行失败** | **#21、#29、#115** | **EULA 必须接受**（Office.conf `AcceptedEULA=true`）+ **多组件模式** `prome_independ` |
| 兼容性 | **#116、#110、#97** | WPS 12.1.0.225xx 后自动化接口失效；ARM64 报错；并发问题 |

## 三、RPC 完整机制（strace + 反汇编还原）

```
client.getWpsApplication() 内部流程：
  1. fork → /usr/bin/wps 脚本（bash 中间层）
  2. bash → office6/wps -automation -rpcserverport=/wpsrpc-<ts>-<rand>
  3. WPS 创建 socket 0_99.0_wps/<pid>-<datetime> 或 wpsrpc-*（wpsd 模式）
  4. 客户端读 wps-daemon-port 文件 → 解析 socket 路径 → connect
  5. QLocalSocket RPC 协议（4字节长度前缀 + payload）
```

**关键发现**：
- `wps-daemon-port` 文件内容格式：`wps.<补零pid>|<datetime>`（11.x 普通文件模式）<br>
  或直接是 socket 文件（wpsd 模式）
- 客户端通过 **PATH 查找 `wps`**（不是绝对路径）→ **PATH 必须含 /usr/bin**

## 四、四版本 WPS 实测对照表

| 版本 | librpcwpsapi_wpsqt.so | librpcserver 加载 | wpsd 行为 | getWpsApplication |
|---|---|---|---|---|
| **11.1.0.11664**（首发） | ❌ 缺 | ❌ | N/A | E_FAIL |
| **11.1.0.11723**（麒麟版） | ✅ 7.2MB | ✅ 4 处 | 自动启动（autologin）| E_FAIL（0.1s） |
| **12.1.0.17900**（SDK 匹配版） | ✅ 8.7MB | ✅ 4 处 | 自动启动（multiply）| E_FAIL（0.1s） |
| **12.1.2.28080**（新版） | ✅ 7.6MB | ✅ 4 处 | 手动可启动 | E_FAIL（曾等待90s） |

## 五、深度调试发现（本次核心进展）

### 5.1 vtable 反汇编（IKRpcClient 虚函数）

```
vt[0]  = registerEvent(IDispatch*, _GUID, uint, void*)            0x7ed460
vt[1]  = registerEvent(IDispatch*, _GUID, ushort*, void*)          0x7ed6e0
vt[2]  = getWpsApplication  ← 0x...fc0（匿名/运行时注入代码，非 weak 桩）
vt[3]  = getEtApplication                                               0x4360c0
vt[4]  = getWppApplication                                              0x4360d0
vt[5]  = setProcessPath                                                 0x7f0240
vt[6]  = setProcessArgs                                                 0x7f0360
vt[7]  = getProcessPid                                                  0x7f0380
vt[8]  = setStartTimeout                                                0x7ef660
vt[9]  = setWpsWide                                                     0x7ef680
vt[10] = isConnected                                                    0x7eb9d0
vt[11] = setAlwaysStartNew                                              0x4360e0
```

### 5.2 setAlwaysStartNew 实现

```asm
0x4360b0 <KRpcClient::setAlwaysStartNew(bool)>:
  mov %sil, 0x18(%rdi)      ; 写 this+0x18
  ret

0x4360e0 <non-virtual thunk to setAlwaysStartNew>:
  mov %sil, 0x8(%rdi)       ; 写 this+0x8
  ret
```

**实测**：`setAlwaysStartNew(true)` 写入 0x01，`setAlwaysStartNew(false)` 写入 0x00——**功能正常**。

### 5.3 getWpsApplication 内部代码（0x9950 区域）

运行时 vtable[2] 指向 **匿名 mmap 代码**（dladdr 失败），跳转到偏移 0x9950 的实现。这是 SDK 内部生成/注入的代码，符号表无对应名称——**这是 WPS Linux SDK 闭源的"接口适配层"**。

### 5.4 客户端进程树（关键）

```
python (主进程)
└─ clone → 380333 (QProcess fork)
    └─ clone → 380547 (/bin/bash /usr/bin/wps)
        ├─ fork → 380548 (bash 内部)
        ├─ fork → 380549
        │   └─ fork → 380550
        └─ execve → 380551 office6/wps -automation -rpcserverport=/wpsrpc-...
```

**QProcess 的子进程（380333）以退出码 1 退出**——这是 SDK 启动失败感知机制的关键。

### 5.5 wps 脚本的 run() 函数（决定性发现）

```bash
function run()
{
    if [ 1 -eq ${gDaemon} ]; then           # -quickstart
        nohup ${gInstallPath}/office6/${gApp} > /dev/null 2>&1 &
    elif [ 1 -eq ${gIsUrl} ]; then          # file://
        ...
    elif [ 1 -eq ${gIsFushion} ] && [ "$1" != "/prometheus" ]; then
        { unset GIO_LAUNCHED_DESKTOP_FILE && ${gInstallPath}/office6/${gApp} /prometheus ...; } > /dev/null 2>&1
    else
        { ${gInstallPath}/office6/${gApp} ${gOptExt} ${gOpt} "$@"; } > /dev/null 2>&1
    fi
}
```

**关键**：默认分支用 `{ office6/wps "$@"; }` **前台子 shell 执行**（bash 必须 fork 子进程来跑管道重定向的子 shell），bash 随后 wait4 等待——这导致 QProcess 检测不到正常启动信号。

### 5.6 WPS 自动重启机制（RestartAppInfo）

Office.conf 有 `wps\Application%20Settings\RestartAppInfo="661253,...,380551,...,400338,..."` 字段记录所有启动过的 PID。当 WPS 检测到崩溃后，**自动用保存的参数重启**——导致测试时出现"幽灵 WPS 实例"（参数是历史 RPC 客户端的）。

**清理方法**：`sed -i 's/wps\\Application%20Settings\\RestartAppInfo=.*//' Office.conf`

### 5.7 wpsd 自动启动机制

WPS 11.1.0.11723 / 12.1.0.17900 启动时会**自动启动 wpsd --component wps**（无需手动）。wpsd 创建 wps-daemon-port **socket**（非普通文件），启动 WPS 工作进程带 `-shield -multiply -automation`。

### 5.8 PATH 问题（极隐蔽）

客户端 SDK 通过 **PATH 查找 `wps` 二进制**（不是绝对路径），如果 PATH 不含 `/usr/bin`，execve 全部 ENOENT，客户端立即返回 E_FAIL。

**修复**：必须在执行环境设置 `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`

### 5.9 客户端从不 connect（终极谜团）

即使：
- ✅ WPS 完全就绪（librpcserver=4）
- ✅ wpsd 启动并 wps-daemon-port socket LISTEN
- ✅ daemon-port 文件已写（含正确格式）
- ✅ PATH 含 /usr/bin
- ✅ setAlwaysStartNew(false) 已调用
- ✅ pywpsrpc 2.4.0 + WPS 12.1.0.17900（版本匹配）
- ✅ EULA 已接受（AcceptedEULA=true 写入 [General] 和 [6.0]）
- ✅ 多组件模式（AppComponentMode=prome_independ）

**客户端仍 0.1s 返回 `0x80000008 (E_FAIL)`**，strace 显示**没有任何 connect 调用**——SDK 在 getWpsApplication 内部有**早期失败检查**绕过了所有连接尝试。

## 六、官方佐证

WPS 官方论坛 `bbs.wps.cn/topic/62639` 确认：**WPS 12.1.0.225xx 之后自动化接口普遍失效**，大量用户投诉 COM/RPC 调用失败。

## 七、本沙箱环境测试结论

| 方案 | 结果 | 说明 |
|---|---|---|
| pywpsrpc 2.4.0 + WPS 11.1.0.11723 | ❌ E_FAIL | wpsd 自动启动但 SDK 早期检查失败 |
| pywpsrpc 2.4.0 + WPS 12.1.0.17900（版本匹配）| ❌ E_FAIL | 完全干净环境下仍立即失败 |
| pywpsrpc 2.4.0 + WPS 12.1.2.28080 | ❌ E_FAIL/卡住 | 曾等待 90s 但未成功 |
| pywpsrpc 2.3.12（任意版本）| ❌ E_FAIL | 同样行为 |
| C 程序直接调用 SDK（绕过 Python）| ❌ E_FAIL | 排除 pywpsrpc 绑定 bug |
| setProcessPath 绕过 bash 脚本中间层 | ❌ E_FAIL | 排除脚本 fork 问题 |
| setAlwaysStartNew(false) | ❌ 不阻止新 WPS 启动 | 客户端仍走相同路径 |
| **GUI 自动化（xdotool）** | ✅ 成功 | 11723 版输出 demo.pdf |

## 八、可交付方案（GUI 自动化流程）

### 8.1 环境准备

```bash
# 系统依赖
apt-get install -y xvfb fluxbox dbus-x11 xdotool x11-utils \
  fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk libtiff5 \
  libqt5gui5 libqt5widgets5 libqt5core5a libxcb-* tesseract-ocr tesseract-ocr-chi-sim

# 安装 WPS 11.1.0.11723
dpkg -i wps-office_11.1.0.11723_amd64.deb
ln -sf /usr/lib/x86_64-linux-gnu/libtiff.so.6 /usr/lib/x86_64-linux-gnu/libtiff.so.5
sed -i 's|Address0=.*|Address0=http://127.0.0.1/blocked|' /opt/kingsoft/wps-office/office6/cfgs/setup.cfg
```

### 8.2 Office.conf 关键配置

```ini
[General]
AcceptedEULA=true
FirstRun=false
UpdateMode=manual

[6.0]
wpsoffice\Application%20Settings\AppComponentMode=prome_independ
wpsoffice\Application%20Settings\AppComponentModeInstall=prome_independ
common\AcceptedEULA=true
common\newInstall=false
# 清理 RestartAppInfo 防止幽灵重启
# （wps\Application%20Settings\RestartAppInfo="..." 整行删除）
```

### 8.3 GUI 导出 PDF 流程（已验证）

```bash
# 1. 启动 WPS（带文档）
setsid bash -c 'DISPLAY=:99 XDG_RUNTIME_DIR=/tmp/runtime-root \
  /opt/kingsoft/wps-office/office6/wps /path/to/demo.docx &'

# 2. 等待 EULA + 字体横幅弹窗
sleep 30

# 3. 关闭字体横幅（不再报告 + 关闭）
xdotool mousemove 855 138 click 1   # 勾选"不再报告"
xdotool mousemove 975 385 click 1   # 关闭

# 4. 文档聚焦 + 文件菜单
xdotool mousemove 640 300 click 1   # 聚焦文档
xdotool mousemove 35 60 click 1     # 文件菜单

# 5. 导出为 PDF（精确坐标：120, 278）
xdotool mousemove 120 278 click 1

# 6. 导出对话框 → 确定
# （不同 WPS 版本坐标略有差异，需 OCR 重新定位）
xdotool mousemove 1216 740 click 1
```

## 九、未来改进方向

1. **生产环境推荐**：桌面级 WPS 11.1.0.9080~11.1.0.11723 + 完整图形会话下用 pywpsrpc 2.4.0
2. **绕过 WPS 的方法**：用 LibreOffice headless `libreoffice --headless --convert-to pdf`（沙箱友好）
3. **等待 WPS 修复**：关注 WPS 官方论坛 #62639 是否解决自动化接口问题
4. **接口适配层研究**：0x9950 区域的运行时匿名代码是 SDK 核心，反汇编它可能找到 E_FAIL 根因

## 十、交付物

- `output/demo.pdf` — 2 页 WPS 文字渲染的 PDF（127KB）
- `pywpsrpc研究报告.md` — 本文档
- `convert_docx2pdf.py` — pywpsrpc 标准 RPC 脚本（WPS 正常时可用）
- `launch_and_convert.py` — Xvfb/fluxbox/dbus/WPS 全托管启动器
- `input/demo.docx` — 测试源文档

**最终建议**：如果必须在 WPS 环境用原生 RPC，优先在桌面级 WPS 11.1.0.9080~11.1.0.11723 且带完整图形会话的环境验证；沙箱无头环境 + 新版 WPS 的组合目前只能走 GUI 自动化。
---

## 🔥 重大更新：RPC 方案已完整跑通（2026-08-15）

> **原结论（GUI 兜底）已过时**。经过深度逆向与修复，**pywpsrpc 原生 RPC 链路在本沙箱环境成功完成 docx→pdf 转换**：
> `getWpsApplication: 0x00000000`（S_OK）、`SaveAs2(PDF): 0x00000000`、输出 `demo_rpc.pdf`（127KB，Creator=WPS 文字，2 页，中文完整）。

### 四、RPC 成功的完整修复链（关键！）

| # | 问题 | 修复方案 |
|---|---|---|
| 1 | **WPS 版本选择** | 必须用 **11.1.0.9662**（2020 年，Qt4 时代）——RPC server 正常激活。12.1.x 自动化接口失效（官方论坛佐证），11.1.0.11723+ 的 RPC server 不 LISTEN |
| 2 | **pywpsrpc 版本** | 9662 的客户端库是 `librpcwpsapi_sysqt5.so`（Qt5 系），需 **pywpsrpc v1.1.0**（2020 年配套绑定）。2.4.0 链接 wpsqt 库与 Qt4 冲突段错误 |
| 3 | **v1.1.0 源码编译** | PyPI 无 cp311 wheel → 源码编译：patch sip 5.5.0（`LAST_SUPPORTED_MINOR=9→11`）+ patch siplib.c（`_frame` 用 `PyFrame_GetBack`） |
| 4 | **`-multiply` 未启用** | `/usr/bin/wps` 脚本 `gOptExt=-multiply` 被注释 → 取消注释（多组件模式 RPC 才激活） |
| 5 | **tablet-mode 探测失败** | SDK 执行 `gsettings get org.ukui.SettingsDaemon...tablet-mode` 退出码 1 → 假 gsettings 脚本（`/usr/local/bin/gsettings` 返回 false/0） |
| 6 | **启动器 exit(1)** | SDK 启动器（setsid+双fork+写PID）正常流程以 exit(1) 结束，客户端误判失败 → **LD_PRELOAD `libexitfix.so`** 把 exit(1)→exit(0) |
| 7 | **seccomp 拦截 mq_open** | 容器 seccomp 过滤 `mq_open(O_CREAT)` 返回 EMFILE → **LD_PRELOAD `libmqsim.so`** 用 **FIFO + poll 模拟** `mq_open/mq_timedreceive/mq_send` 等全套接口 |
| 8 | **setStartTimeout 单位** | v1.1.0 参数是**微秒**：`setStartTimeout(15000000)` = 15 秒（写 30000 只有 30ms 必失败） |
| 9 | **API 命名** | v1.1.0 用 `get_Documents()`/`get_ActiveDocument()`（属性风格不可用） |

### 五、RPC 转换最终脚本

```bash
# 一键转换（已封装）：
/workspace/docx2pdf/rpc/run_convert.sh <输入.docx> <输出.pdf>
```

```python
# 核心代码（convert_docx2pdf.py）
from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.common import S_OK, QtApp

qApp = QtApp(sys.argv)
hr, rpc = createWpsRpcInstance()
rpc.setStartTimeout(15000000)          # 微秒 = 15s
hr, app = rpc.getWpsApplication()      # 0x00000000 ✅
hr, docs = app.get_Documents()
hr, doc = docs.Open(src)
hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)   # 0x00000000 ✅
```

### 六、交付文件清单（/workspace/docx2pdf/）

```
output/demo.pdf         # GUI 方案产物（早期验证）
output/demo_rpc.pdf     # ★ RPC 方案产物（最终交付，Creator=WPS 文字）
rpc/convert_docx2pdf.py # RPC 转换脚本
rpc/run_convert.sh      # 一键转换脚本
rpc/libexitfix.so/.c    # exit(1)→exit(0) 修复库（含源码）
rpc/libmqsim.so/.c      # FIFO mq 模拟库（含源码，绕过 seccomp）
input/demo.docx         # 测试源文档
```

### 七、遗留说明

- 修复链针对**沙箱无头环境**（seccomp 拦 mq_open、无 UKUI 桌面、WPS 12 自动化失效）定制；**在正常桌面 Linux + WPS 11.1.0.9662 上，只需第 1/2/4 项**（版本匹配 + -multiply）即可跑通，无需 LD_PRELOAD。
- 后续版本（11723+/12.x）的自动化接口问题需 WPS 官方修复。

---

## 🐳 Docker 容器化交付（2026-08-15 已端到端验证）

> **`docker run --rm -v $PWD:/data wps-docx2pdf input.docx output.pdf` 一键转换，验证通过**：
> `getWpsApplication: 0x00000000`、`SaveAs2(PDF): 0x00000000`、输出 183059 字节（Creator=WPS 文字，2 页，中文完整），exit=0。

### Docker 调试中新增的 2 个关键修复点

| # | 问题 | 修复方案 |
|---|---|---|
| 10 | **DBUS 地址硬编码覆盖** | 脚本曾硬编码 `DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/runtime-root/dbus`，覆盖 entrypoint 里 `dbus-launch` 生成的真实地址（`unix:path=/tmp/dbus-<随机>`）→ WPS 连不上会话总线直接段错误。改为 `os.environ.setdefault(...)`：沿用 entrypoint 注入的真实地址，仅缺失时兜底 |
| 11 | **`main()` 从未执行** | docker 版脚本定义了 `main()` 但末尾漏写 `if __name__ == "__main__": main()` → 脚本 import 完就正常退出，Python 退出清理 Qt/sip 时段错误（无任何输出）。补上入口后 `os._exit()` 跳过清理正常交付 |

### 容器架构（两阶段构建，镜像 ~2.17GB）

```
builder 阶段（ubuntu:24.04 + build 工具链）
  ├─ sip 5.5.0（patch Py3.12）+ pywpsrpc v1.1.0 源码编译
  ├─ 从 WPS deb 提取 librpcwpsapi_sysqt5.so 供链接
  └─ 编译 libexitfix.so / libmqsim2.so（LD_PRELOAD 修复库）
runtime 阶段（ubuntu:24.04 + 运行时依赖）
  ├─ Xvfb / fluxbox / dbus / Qt5 全套 / 中文字体
  ├─ WPS 11.1.0.9662（阿里云 ubuntukylin 镜像下载）
  ├─ libtiff.so.5 符号链接 + 假 gsettings + 屏蔽更新服务器
  ├─ Office.conf（AcceptedEULA + prome_independ + 清理 RestartAppInfo）
  └─ /usr/bin/wps 启用 -multiply + exec 直接启动
```

### 使用方式

```bash
cd /workspace/docx2pdf/docker
docker build -t wps-docx2pdf .
docker run --rm -v "$PWD":/data wps-docx2pdf input.docx output.pdf
# 不传参数时默认 /data/input.docx → /data/output.pdf
```

### Docker 交付文件清单（/workspace/docx2pdf/docker/）

```
Dockerfile            # 两阶段构建（含全部 11 项修复）
entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换（失败也打印退出码）
convert_docx2pdf.py   # RPC 转换脚本（含 main 入口 + setdefault 环境）
README.md             # 构建/使用/验证说明
pywpsrpc-src.tar.gz   # pywpsrpc v1.1.0 源码 + wpsrpc-sdk
libexitfix.c          # 修复库①源码
libmqsim.c            # 修复库②源码
```

### 最终交付物（/workspace/docx2pdf/）

```
output/demo_rpc.pdf          # ★ RPC 方案产物（原沙箱，127KB，Creator=WPS 文字）
output/demo_rpc_docker.pdf   # ★ Docker 容器产物（183059 字节，Creator=WPS 文字，2 页）
rpc/                         # 原沙箱 RPC 一键转换方案
docker/                      # 容器化交付方案（推荐）
```

---

## 🔥🔥 WPS 12 验证成功 + 备用镜像（2026-08-15 二次更新）

> **修正此前过时结论**：文中之前"WPS 12.1.0.225xx 之后自动化接口失效"的说法**不准确**。
> 实测 **WPS 12.1.2.28080 + pywpsrpc 2.4.0 在 Docker 中三组件（wps/wpp/et）RPC 驱动 + 转 PDF 全部成功**。
> 此前沙箱原生 E_FAIL 的根因是 **沙箱 seccomp 拦截 `mq_open`**（9662 在沙箱原生同样失败），与 WPS 12 本身无关。

### 新发现（本轮逆向/实测）

1. **WPS 12 的 RPC SDK 库改名**：`librpc*_sysqt5.so`（9662）→ `librpc*_wpsqt.so`（12）。
   - pywpsrpc 1.1.0 写死找 `_sysqt5` → 在 12 下 import 阶段直接失败
   - **pywpsrpc 2.4.0 的 `project.py` 按 `["wpsqt","sysqt5"]` 顺序探测** → 编译时自动链接 12 的 `_wpsqt`
2. **RPC 通信机制（strace 还原）**：客户端 `KRpcClient::startExe` 先 `mq_open(/wpsrpc-<ts>-<rand>)`
   （POSIX 消息队列，非 socket）→ 再经 **daemon 中转**（`connectServerViaDaemon` +
   `~/.local/share/Kingsoft/daemon/wps-daemon-port`）；
   沙箱 seccomp 拦 `mq_open` → 客户端 0.1s 返回 `0x80000008`（无任何 connect 调用）。
3. **pywpsrpc 2.4.0 编译要点**：sip 版本敏感——6.15+ 改 API 编译崩、6.5.x 不支持 Py3.11+ ABI，
   **sip 6.8.3 正好**；`sip-build` 一条命令，不再需要 v1.1.0 的 sip 5.5 patch / siplib.c patch。
4. **WPS 12 deb**：官网 Personal 版 571MB（9662 的 301MB），下载走动态签名
   （`?t=<秒>&k=MD5(key+uri+t)`，key 从 linux.wps.cn 页面源码提取），封装为 `download_wps12.sh`。
5. **postinst**：WPS 12 的 postinst 需要 `hexdump`（bsdmainutils），缺失报 exit 127（包已解包，可容忍）。

### 验证数据（Docker 内实测）

| 组件 | getApplication | 新建→转 PDF |
|---|---|---|
| wps（文字） | S_OK（0.5s） | ✅ 967B |
| wpp（演示） | S_OK（0.3s） | ✅ 733B |
| et（表格） | S_OK（0.4s） | ✅ 19960B |

### 交付：备用镜像（docker/Dockerfile.wps12）

- 与主镜像共用 `entrypoint.sh` / `http_server.py` / `convert_docx2pdf.py`（API 兼容），**主镜像零改动**
- 构建：`docker build -f Dockerfile.wps12 -t wps2pdf-wps12 .`
  （默认官网签名下载；或 `--build-arg WPS_DEB_URL=<GitHub Release URL>` 走自家 CDN）
- 备用镜像 **3.08GB**（主镜像 2.07GB）；切换触发场景：9662 deb 源失效 / 新版文档渲染异常
- 相关文件：`Dockerfile.wps12`、`download_wps12.sh`、`pywpsrpc-2.4.0-src.tar.gz`

### 重要修正（对照文中旧表述）

- 四版本对照表中 12.1.0.17900 / 12.1.2.28080 的 E_FAIL 均为**沙箱 seccomp 环境导致**；
  在 Docker（默认 seccomp 允许 mq_open）中 12.1.2.28080 完全可用
- 官方论坛 #62639"自动化失效"与 **库名/版本匹配** 有关：新版 WPS 需配套新版 pywpsrpc（1.1.0→2.4.0）

---

## 🔥🔥🔥 主镜像升级：pywpsrpc 1.1.0 → 2.4.0（2026-08-15 三次更新）

> 实测：**WPS 11.1.0.9662 + pywpsrpc 2.4.0 三组件转换全部通过**
> （wps 966B / wpp 732B / et 15379B），且 **libexitfix 不再需要**（只 preload libmqsim2 依然 OK）。

### 结论

- **WPS 11 不可升级**：9662 是 11 系列唯一 RPC 正常版（更新的 11.1.0.11723 实测 RPC server 不监听）
- **pywpsrpc 可单独升级到 2.4.0**：2.4.0 的 `_detect_rpc_lib` 按 `["wpsqt","sysqt5"]` 探测，
  9662 的 office6 只有 `_sysqt5` → 自动降级链接（`ldd` 实测确认 `librpcwpsapi_sysqt5.so`）
- **libexitfix 弃用**：启动感知修复在 pywpsrpc 2.4.0 客户端侧（与 WPS 版本无关），
  9662 / 12 两个镜像的 entrypoint 均只 preload `libmqsim2.so`

### 主镜像变更（v1.3.0）

- Dockerfile builder：sip 5.5.0 + 三个 patch（py_versions / siplib.c / IKRpcClient.sip）→ **sip 6.8.3 + `sip-build`**（一条命令，无需 patch）
- 源码包：`pywpsrpc-src.tar.gz`（1.1.0，11MB）→ `pywpsrpc-2.4.0-src.tar.gz`（2.2MB）
- `entrypoint.sh` / `tests/e2e_test.sh`：`LD_PRELOAD` 只留 `libmqsim2.so`（`libexitfix.so` 保留文件备回退）
- 其余（WPS 9662 安装、Office.conf、`-multiply`、gsettings、libtiff、字体）不变

---

## 🔥🔥🔥🔥 并发能力验证（2026-08-16）

> 背景：API 模式当前是**单例串行**（每组件一个 WPS 实例 + 锁，0.13s/请求，且 async 端点同步调 RPC 会 pin 死 uvicorn 事件循环）。问题：能否支持并发？结论先行：**WPS 实例不能多线程/多进程共享，并发只能靠多实例；多实例"串行初始化 + 并行转换"完全可行且稳定。**

### 实验一：5 实例同时冷启动（并发初始化竞态 ❌）

同一容器内 5 个转换进程**同时**从零创建 WPS 实例（3 docx + 1 pptx + 1 csv）：

| 任务 | 组件 | 结果 | 耗时 |
|---|---|---|---|
| doc1 | wps | ✅ 成功 | 15.4s |
| doc2 | wps | ❌ `getWpsApplication: 0x80000008` | 30s |
| doc3 | wps | ✅ 成功 | 0.8s |
| pptx | wpp | ❌ `getWppApplication: 0x80000008` | 30s |
| csv | et | ❌ 初始化挂起 → 180s 超时 | 180s |

**结论：并发冷启动存在明显竞态**（RPC 握手 / 配置初始化互相干扰），5 例 3 败（E_FAIL 或挂起）。不能无脑多进程同时初始化。

### 实验二：worker 池模式（串行初始化 + 并行转换 ✅）

模拟真实 worker 池：3 个 worker **错开 2s 依次启动**（就绪后驻留），全部 READY 后**同时派发** 3 个转换：

- 初始化：3/3 READY，零失败（串行化消除了竞态）
- 并行转换：3 个任务全部 DONE，**总耗时 1004ms**（单任务 0.14–0.15s，总耗时 ≈ 单任务耗时 = 真并行）

**结论：实例初始化必须串行，转换任务可并行派发，吞吐线性。**

### 压力测试：4 worker × 15 任务 = 60 任务（2026-08-16）

4 worker（wps×2 + wpp + et）错开 2s 串行初始化，60 个混合任务并行派发：

- **60/60 成功，0 失败，0 重试**，总耗时 **1s**
- 单任务耗时：首任务 194–444ms（文档冷启动），之后稳定 16–107ms
- 每个 worker 连续 15 个任务无退化（第 15 个仍 ~70ms）——无状态累积/内存泄漏迹象

### 隔离测试：ODP 死锁注入（2026-08-16）

w1/w2（wps，各 22 任务）+ w4（et，首任务注入最小 ODP 触发已知死锁）+ w3（wpp）：

- **w4 永久挂死**（ODP 导入 bug，无任何输出，符合预期）
- **w1/w2 完全不受影响**：44/44 任务全部成功 → **卡死隔离成立**（worker 池相对单例的核心优势：一个实例卡死不拖垮全服务）
- **意外发现**：w3（wpp 组件）因测试脚本误配了 docx 任务同样**永久挂起**（60s 无输出）——WPS 组件 Open **类型不匹配**的文档也会挂死，不只是 ODP。**⇒ 单任务超时兜底必须覆盖每个任务，且不能依赖快速失败。**

### 实测要点（对 worker 池实现的设计约束）

| 设计点 | 结论 |
|---|---|
| 实例初始化 | **必须串行**，worker 间错开 ≥2s |
| 任务派发 | 并行，无共享状态 |
| 单任务超时 | **必须子进程隔离 + 超时 SIGKILL**；RPC 卡死时 `signal.alarm` 无法中断 C 扩展（sip）阻塞，只能杀进程（CLI 的 multiprocessing 方案即此） |
| worker 卡死 | kill **整个进程组**（实测 python worker 退出后 WPS 实例进程会残留）→ 串行重建 |
| 吞吐 | 实测线性：4 worker ≈ 4× 单例（60 任务 1s） |

### 状态

- 并发方案已对比：多容器 + 负载均衡（零代码，容器级隔离） vs 单容器 worker 池（改 http_server.py，worker 级隔离 + 顺带解决 API 超时顽疾）
- 结论已记录；worker 池落地实现（目标 v1.6.1）待定

### 内存占用实测（2026-08-16）

容器内分阶段抓 RSS（`ps -eo rss`）：

| 阶段 | WPS 相关合计 | 容器内全部 RSS | 说明 |
|---|---|---|---|
| 环境基准（无 WPS） | 0 MB | 98 MB | Xvfb 67MB + fluxbox 13MB + dbus 2MB |
| 1 个 wps 实例就绪 | 519 MB | 643 MB | wps 主进程 **365MB** + wpsoffice 154MB |
| 4 个实例就绪（wps×2+wpp+et） | **1200 MB** | 1775 MB | wps 365×2 + wpp 311MB + wpsoffice 155MB |
| 4 worker 并行转换中 | 1200 MB | 1775 MB | 与就绪态完全一致 |
| 转换结束后 | 1200 MB | 1775 MB | 内存回落/无增长 |

**关键结论**：

- **单实例成本**：wps ≈ **365MB**、wpp ≈ **311MB**（et 量级相近，未单独拆测）；每实例 ≈ 0.3–0.4GB
- **转换过程零额外常驻**：就绪态 → 转换中 → 结束后 RSS 恒为 1200MB，无峰值膨胀、无泄漏（与压力测试 60 任务后无退化互相印证）
- Xvfb 随 WPS 窗口数增长（67 → 123MB，多实例共享 :99 display）
- **worker 池内存预算**：N worker ≈ N×0.35GB + 0.1GB 环境；8GB 容器可跑 ~20 worker，12GB 可跑 ~30
- 再次确认：python worker 退出后 WPS 引擎进程**残留**（[2] 阶段 worker 已退出但 WPS 仍在）——实现需进程组级清理

### 冷启动成本实测（2026-08-17）

**目的**：澄清"按需拉起实例"（来一个文档、该组件实例不存在时现场拉起再转）的真实时间成本，与上面的"并发初始化竞态"区分。

**方法**：容器内启动 Xvfb/fluxbox/dbus 后，对每个组件串行测量：①首次冷启动（实例从零 spawn：reset→_ensure→convert）②稳态冷启动×3（reset 后重连已有全局 WPS 服务）③热转换×6（实例就绪，纯 Open+Export）。每个 _ensure/convert 包在子进程内 + 60s 超时强杀兜底（避免实验一已知的"冷启动 getApplication 偶发永久阻塞"卡死脚本）。

**结果**：

| 组件 | 首次冷启动<br>(拉起+转) | 稳态冷启动<br>(reset 重连) | 热转换<br>(常驻均值) | 纯拉起成本 |
|---|---|---|---|---|
| wps | 753ms (448+305) | ~378ms (227+151) | 61ms | 227ms ≈ 热 3.7× |
| wpp | 782ms (396+386) | ~401ms (219+182) | 57ms | 219ms ≈ 热 3.8× |
| et  | 954ms (317+637) | ~528ms (208+320) | 139ms | 208ms ≈ 热 1.5× |

（热转换首次 152/184/332ms 偏慢，系"首次 Open 该文档"的字体/解析初始化开销，之后稳定 35–140ms。）

**结论**：

- **串行按需拉起成本仅 ~200–450ms**（首次 spawn 稍贵 ~450ms，稳态重连 ~220ms），远低于之前"冷启动要几秒"的笼统印象——那句是**并发竞态语境**（实验一 5 实例同时起，被竞态拖到 15.4s 甚至挂死），二者不矛盾。
- **冷启动使单次请求多花 0.3–0.9s**：常驻热转换 0.05–0.14s，首次冷启动 0.75–0.95s，稳态冷 0.4–0.5s。
- **对架构选型的影响**：低频/按需场景（如 CLI 每容器转换一次）"用完即焚"完全可接受；高频场景"常驻实例"省下的就是这几百毫秒/请求——当前单例 API 的"常驻不关"设计，收益正是省去这 ~200–450ms 拉起成本。
- 实测脚本末尾 WPS 进程数累积到 17（每子进程结束未清理残留 WPS 实例，与上面"残留"结论一致）——再次佐证实现需进程组级清理。

---

## 🔧 v1.6.1 Worker 池实现蓝图（讨论定稿 2026-08-17）

### 目标

解决当前单例 API 的三个结构性问题：①不能并发（锁串行化，同一时刻每组件只能转 1 个）②空转吃内存（三组件全启动后约 1.2GB 常驻，零请求也占）③卡死拖垮全局（ODP 死锁只能靠容器级 `timeout` 兜底，且事件循环被同步 RPC pin 死 → daemon 线程超时失效）。

**定位**：单容器、单端口、零外部编排；worker **按需创建、弹性伸缩**（每来一个请求，有同类型空闲 worker 就复用，没有就新建），无预配置容量；顺带根治 API 超时顽疾（主进程事件循环不再被阻塞 RPC 占用）。

### 架构

```
主进程 (FastAPI, async —— 只做 IO 收发，绝不碰 RPC)
  └── WORKERS: 动态列表 [(pid, type, status, handler)]   # 无预创建, 容量由并发请求驱动
        │
        │  每请求: worker = find_idle(type) or create_worker(type)
        ▼
  Worker(子进程, 持有常驻 wps 实例) —— 处理完本请求后:
        若有其他同类型 idle → killpg 自杀回收
        否则                → 转 idle 备用
```

**关键约束**：真实 WPS 实例活在 worker 子进程里；主进程只持"逻辑句柄"（队列 + `pid`），**不能**把实例对象直接塞进主进程（跨进程不能共享对象）。worker 与请求 1:1 绑定、结束后按 spare 策略回收，故**无需 watchdog / 重建 / 固定槽位**。

### 数据结构

```python
class Worker:
    handler:   逻辑句柄 (task_q_in, result_q_out, pid)   # 主进程侧指挥通道
    type:      "wps" | "wpp" | "et"
    status:    "busy" | "idle"        # idle = 就绪备用; 正在执行本请求时为 busy
    pid:       int     # worker 子进程 pid (killpg 用)

WORKERS: list[Worker] = []            # 动态列表, 无预创建; 容量完全由并发请求驱动
SPARE_PER_TYPE: int = 1               # 每类型最多保留几个 idle 备用 (设 0 = 完全不缓存, 来一个建一个)
```

> 没有固定槽位数组、没有 `WORKER_CONFIG`、没有 `dead`/重建态——worker 用完即焚或留作备用，容量天然弹性。

### 状态机

```
  (请求到达) ── find_idle(type) ──▶ 复用 idle worker ──┐
        │                                            │
        └── create_worker(type) ──▶ busy ──(完成)──┬──┴─▶ 统计同类型 idle 数(不含自己):
                                                       ≥ SPARE_PER_TYPE → killpg 自杀(回收)
                                                       否则            → 转 idle 备用
                    busy ──(请求超时: asyncio.wait_for)──▶ killpg 自杀(不保留)
```

- **不用 `heartbeat`**：idle 的 worker 没在跑任务、不会卡死，无需监控；只有 busy 的 worker（被某请求占用）才可能在处理任务时卡死，超时由**该请求自身的定时器**判定，无需全局心跳扫描。
- 状态由**主进程**维护（`find_idle` 取出即标记 `busy`；完成 `busy→idle` 或回收；超时 `busy→kill`）。worker 子进程只领任务、回传结果，**不汇报心跳**。
- 超时触发即"请求级"：`await asyncio.wait_for(dispatch(worker, task), timeout=TASK_TIMEOUT)`，事件循环本就在跑，零额外监控成本；超时回调里 `os.killpg(pid, SIGKILL)` 连残留 WPS 一起清，返回该请求 504，不保留（下次请求重新 `create_worker`）。

### 关键流程与做法

| 环节 | 做法 |
|---|---|
| **派发** | 按 `type` `find_idle`（**原子取出**并立即标记 `busy`，防并发两请求抢同一 worker）；无空闲则 `create_worker` 新建（冷启动，内部带 `ensure_retry` 重试兜底） |
| **执行** | worker 进程内直接 RPC（单实例 + 进程内锁串行） |
| **成功回收** | 请求完成 → 统计同类型 `idle` 数（不含自己）；≥ `SPARE_PER_TYPE` 则 `os.killpg(pid)` 回收自身（连残留 WPS 一起清），否则转 `idle` 备用 |
| **卡死/超时** | 请求级 `asyncio.wait_for` 超时 → `os.killpg(pid)`（连残留 WPS 一起清）→ 返回该请求 504；**不保留**（下次请求重建） |
| **清理** | **进程组级**（`os.killpg`）——实测 worker 退出后 WPS 实例进程会残留，仅 kill worker 壳清不干净 |

### ⚠️ 已拍板的关键抉择：handler = 常驻活实例；动态创建 + spare 回收（非固定池）

- **handler = 常驻活实例**（跨任务复用，单次走热路径 50–140ms），实例活在 worker 进程内、**无法跨到临时子进程使用**（跨进程连同一全局 WPS 单例会竞态，已验证）→ 放弃"每任务临时子进程"。
- **不再"固定池 + 重建"**：worker **动态创建、用完回收**，无 `WORKER_CONFIG`、无槽位、无重建逻辑。容量为弹性：并发度天然 = 同时到达的请求数，完成后自动收敛到每类型 `SPARE_PER_TYPE` 个 idle 备用。
- 一次 ODP 卡死 → 只 `kill` 该 worker（请求返回 504），**下次请求重新 `create_worker`**；其他请求/worker 完全不受影响（隔离测试已证 44/44 正常）。
- 这套模型天然消解了旧 API 超时顽疾：主进程 async 不再被同步 RPC 阻塞，超时交给"请求级 `asyncio.wait_for` 对 worker 进程 `kill`"，而非在事件循环里 `join`；且无需独立 watchdog。

### 超时与复用配置

| 配置项 | 取值 | 说明 |
|---|---|---|
| 初始化超时 | 60s | 冷启动 `getApplication` 会**永久阻塞**（非返回错误），`create_worker` 内必须用子进程超时强杀，不能靠 `signal.alarm`（C 扩展 sip 阻塞无法被信号中断） |
| 通用任务超时 | 180s | 对齐 CLI 的 `multiprocessing` + SIGKILL 兜底 |
| ODP 专项超时 | 30s（建议更激进） | ODP 已知必死，缩短触发回收、少留 spare |
| 超时触发机制 | per-request `asyncio.wait_for` | 无需独立 watchdog / 心跳扫描；仅 busy（被请求占用）worker 可被超时 |
| 复用策略 `SPARE_PER_TYPE` | 1（环境变量可配） | 每类型最多保留几个 idle 备用；设 0 = 完全不缓存（来一个建一个，空闲零常驻） |

### 内存预算

- 单实例 ≈ 0.3–0.4GB（实测：wps 365MB / wpp 311MB）。
- **空闲（无请求）**：每类型 `SPARE_PER_TYPE×0.35GB`；默认 3 类型 ×1 = **~1GB 常驻**（比当前单例三组件全起 1.2GB 还略低）。设 `SPARE_PER_TYPE=0` → 空闲零常驻。
- **忙时**：随并发增长（N 并发 ≈ N×0.35GB + spare）。8GB 容器可支撑 ~20 并发，12GB ~30。并发能力几乎不受内存限制。

### 与现状的衔接（复用，不重写）

- `RpcEngine._ensure / convert / reset` 逻辑直接进 worker 子进程（已是进程内代码）；`ensure_retry` 重试兜底盘进 `create_worker`。
- CLI 的 `multiprocessing` + 超时 SIGKILL 兜底思路 → 复用到 `create_worker` 初始化与请求级超时回收。
- `http_server.py` 改造：主进程 FastAPI + 动态 `WORKERS` 管理（`find_idle`/`create_worker`/回收）+ 请求级 `asyncio.wait_for` 超时；`/convert` 改为取/建 worker、`await` 结果（async，不再同步阻塞）；`/health` 暴露各类型 idle/忙数。
- `entrypoint.sh` 的 `timeout` 兜底保留（最终防线）。

### 落地步骤（实现清单）

1. 新增 `worker.py`：worker 子进程（持有 `RpcEngine`，循环 收任务→RPC→回报结果）。
2. 改造 `http_server.py`：主进程 FastAPI + 动态 `WORKERS` 管理（`find_idle` 原子取出 / `create_worker` 带重试 / 成功回收 / 请求级 `asyncio.wait_for` 超时 kill）；`SPARE_PER_TYPE` 环境变量可配（默认 1）。
3. e2e 测试扩展：并发压测（验证并发 `create_worker` + 完成后收敛到 ≤SPARE_PER_TYPE idle）+ ODP 隔离（注入死锁验证其他请求自动重建不受影响）。
4. README 更新：并发能力说明 + 复用策略（`SPARE_PER_TYPE`）+ 内存/吞吐参考。

### 风险与注意

- **取 idle 必须原子**：`find_idle` 取出即标记 `busy`，避免两个并发请求抢到同一个 idle worker。
- **动态 `create_worker` 的并发冷启动竞态**（实验一 5 同时起 3/5 失败）→ `create_worker` 内部带 `ensure_retry` 重试兜底；可选加"创建信号量"限制同时冷启动数。即便某个冷启动失败也不影响其他请求。
- 单任务超时必须**进程组级**（wpp 组件 Open docx 等类型不匹配也会永久挂死，不止 ODP）→ 超时兜底覆盖所有任务。
- 跨进程不能共享 WPS 实例对象——主进程只持逻辑句柄（队列 + pid）。
- 线程方案不可取：Python 无法强杀线程，卡死线程变僵尸、实例无法回收、残留累积——必须用进程（+ `killpg`）。

### 决策记录（讨论定稿）

- 并发方案选定**自研 worker 池**（非多容器/nginx，非 gunicorn）——理由：多容器/多 worker 的"浪费"在 (N-1)×0.1GB 环境冗余 + 容器/镜像/编排放大；worker 池单容器单端口零编排，且天然根治卡死/超时。
- handler 语义 = **常驻活实例**（跨任务复用，省冷启动）。
- 每 worker **独立进程**（OS 级强杀才能干净处理不可逆死锁；线程无法强杀）。
- Python 不改 Node.js：核心依赖 `pywpsrpc` 是 Python 专属私有 RPC 客户端，Node 无等价物；换语言零收益、高移植风险。
- 不采用 heartbeat + 独立 watchdog 扫描（2026-08-17 讨论精简）：idle worker 未跑任务不会卡死，只需在 busy（被请求占用）时由该请求的 `asyncio.wait_for` 超时判定 → killpg。
- worker 采用**动态创建 + 每类型最多 1 个 idle 备用**的弹性模型（2026-08-17 重构）：取代固定槽位数组 `WORKER_CONFIG` + 重建逻辑。本质 = 每请求建一个 worker、用完即焚；"复用/留备用"仅为省冷启动的缓存优化，非必需。无预配置容量、无 watchdog、无重建；并发度天然 = 同时到达的请求数，完成后自动收敛到 ≤`SPARE_PER_TYPE` idle/类型。
