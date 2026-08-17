# wps2pdf：Office / WPS 文档一键转 PDF（Docker 镜像）

[![CI](https://github.com/xna00/wps-docker/actions/workflows/build.yml/badge.svg)](https://github.com/xna00/wps-docker/actions)

用 **WPS 11.1.0.9662 + pywpsrpc v2.4.0 原生 RPC** 将 Office/WPS/ODF 等 **16 种格式**文档转为 PDF 的 Docker 方案。内置 20 个中文字体 + 思源黑体（Noto Sans SC），并内置"微软雅黑/等线/Calibri"等缺失字体名 → 思源黑体/Carlito 的映射，支持 CLI 与 HTTP API 两种使用方式。

## 快速开始

> 直接使用 **Docker Hub 发布镜像** [`xna00/wps2pdf`](https://hub.docker.com/r/xna00/wps2pdf)（`latest` 跟随版本更新，无需本地构建），`docker run` 首次运行会自动拉取；也可先 `docker pull xna00/wps2pdf` 显式拉取最新版。

### 方式一：CLI（一条命令转换）

```bash
# 输入/输出挂载到 /data，output.pdf 出现在当前目录
docker run --rm -v "$PWD":/data xna00/wps2pdf input.docx output.pdf

# 不传参数时默认 /data/input.docx → /data/output.pdf
docker run --rm -v "$PWD":/data xna00/wps2pdf
```

支持格式（按扩展名自动路由到 wps/wpp/et 三组件）：

| 组件 | 格式 |
|---|---|
| 文字（wps） | docx doc wps rtf txt xml html htm mht mhtml odt uot uof dot dotx |
| 演示（wpp） | pptx ppt dps pot potx odp uop pps ppsx |
| 表格（et） | xlsx xls et csv ods uos xlt xltx ett prn dif |

### 方式二：HTTP API（常驻服务）

```bash
# 启动（端口默认 8080）。建议加 --init：tini 作 PID 1 回收 worker 强杀后
# 残留的 WPS 孤儿/僵尸进程，避免长期运行 PID 槽被耗尽
docker run -d --init --name wps-api -p 8080:8080 -e MODE=api xna00/wps2pdf

# 健康检查（v1.7.0：workers 按类型统计 busy/idle，total = 当前存活 worker 数）
curl http://localhost:8080/health
#   {"status":"ok","workers":{"wps":{"busy":0,"idle":1}},"total":1,"max_workers":3}

# 上传文档 → 返回 PDF
curl -o output.pdf -F "file=@input.docx" http://localhost:8080/convert

# 可选环境变量
#   PORT=8080         监听端口
#   MAX_FILE_MB=50    上传大小上限（默认 50MB）
#   MAX_WORKERS=3     全局并发 worker 上限（0=不限制；按内存预算调，每 worker ≈0.4GB）
#   QUEUE_TIMEOUT=60  池满排队超时秒（超时返回 503）
#   TASK_TIMEOUT=180  转换超时秒（兜底 WPS 挂死）
```

> 说明（v1.7.0）：**并发 worker 池**架构——请求动态创建 WPS worker（每实例独立进程，可并行），
> 用完保留 1 个 idle 备用，冷启动跨进程文件锁串行化，请求级超时强杀 + 进程级挂死隔离。
> 实测：小文件并发与旧版持平（~10 QPS），大文件并发快 2~3.4 倍；已知挂死格式
> odp/ods/uop 入口 415 直接拒绝；池满排队超时返回 503。内存 ≈ MAX_WORKERS×0.4GB。
> **仅限内网使用，无鉴权**（如需公网暴露请自行加网关鉴权）。

## 构建

> 快速开始已直接使用发布镜像，无需构建。以下为**自行构建**场景（离线环境、改字体/依赖、验证 WPS 12 备用镜像等）；本地构建产物把上述命令里的 `xna00/wps2pdf` 换成 `wps2pdf` 即可同样运行。

```bash
docker build -t wps2pdf .
```

> 所有外部依赖统一走**阿里云镜像**（apt / pip / WPS deb），国内构建快，境外 runner 同样可达。
> WPS deb（~301MB）超 GitHub 100MB 单文件上限，不能入库，构建时远程拉取；若阿里云 URL 失效，
> 手动下载 deb 放本目录，把 Dockerfile 中两处 `curl .../wps.deb` 改为 `COPY wps-office_*.deb /tmp/wps.deb` / `COPY ... /tmp/wps-sdk.deb`。

### 可选构建参数（build-arg）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `WPS_DEB_URL` | 阿里云 ubuntukylin 完整 deb URL | WPS deb 下载地址。GitHub CI 会覆盖为仓库 Release 附件（自家 CDN 更快）；本地默认走阿里云 |
| `FONTS_URL` | `github.com/.../releases/download/wps-11.1.0.9662/fonts.tar.xz` | 中文字体包（57MB）下载地址。CI 境外直连 GitHub CDN 秒级；**国内本地构建**建议覆盖为加速代理（实测 `https://ghfast.top/https://github.com/xna00/wps-docker/releases/download/wps-11.1.0.9662/fonts.tar.xz` 约 10s） |

## 验证

```bash
pdfinfo output.pdf        # Creator 应为 "WPS 文字"，Pages 2
pdftotext output.pdf -    # 中文内容完整

# 端到端自动化测试（生成最小 docx → RPC 转换 → 断言 PDF 产物/页数）
docker run --rm --entrypoint /bin/bash xna00/wps2pdf \
  -c "/opt/wpsrpc-rpc/tests/e2e_test.sh"
```

## WPS 12 备用镜像（Dockerfile.wps12）

主镜像（9662 + pywpsrpc 2.4.0）稳定运行中；`Dockerfile.wps12` 提供 **WPS 12.1.2.28080 + pywpsrpc 2.4.0** 的备用构建路径，用于：
- 9662 的 deb 源（阿里云 ubuntukylin）失效导致主镜像无法构建时
- 新版文档（Office 365 等）在 9662 下渲染异常、需要新版解析内核时

**与主镜像的差异**：

| 项 | 主镜像（Dockerfile） | 备用镜像（Dockerfile.wps12） |
|---|---|---|
| WPS | 11.1.0.9662（301MB deb，固定 URL） | 12.1.2.28080（571MB deb，官网动态签名下载） |
| RPC SDK 库 | `librpc*_sysqt5.so` | `librpc*_wpsqt.so`（12 改名） |
| pywpsrpc | v2.4.0（sip 6.8.3 + `sip-build`，自动探测 `_sysqt5`） | v2.4.0（sip 6.8.3 + `sip-build`，自动探测 `_wpsqt`） |
| 镜像体积 | ~2.1GB | ~3.1GB |

构建：

```bash
# 默认：download_wps12.sh 从官网 wpscdn 动态签名下载（需外网）
docker build -f Dockerfile.wps12 -t wps2pdf-wps12 .

# 或指定 Release 附件 URL（GitHub 自家 CDN，更稳）
docker build -f Dockerfile.wps12 -t wps2pdf-wps12 \
  --build-arg WPS_DEB_URL=https://github.com/<owner>/<repo>/releases/download/<tag>/wps-office_12.1.2.28080.AK.preread.sw.Personal_765474_amd64.deb .
```

已验证：三组件（wps/wpp/et）RPC 驱动 + 新建→转 PDF 全部通过；共享 `entrypoint.sh`/`http_server.py`/`convert_docx2pdf.py`，API 与主镜像完全兼容。修复库：`libmqsim2.so` 对 12 **同样必需**；`libexitfix.so` 对 pywpsrpc 2.4.0 已非必需（两镜像均只 preload `libmqsim2.so`）。

> **注意**：12 为官网 Personal 版，商用需自行确认授权（与 9662 个人版同理）。

## 背景（为什么需要这个组合）

- WPS **11.1.0.9662（2020）RPC server 正常**（更新的 11723+ 不 LISTEN）；12.1.2.28080 也可用（SDK 库改名 `_wpsqt`，需 pywpsrpc 2.4.0）
- 9662 配套 **pywpsrpc v2.4.0**（`project.py` 自动探测 `_sysqt5`，本镜像三模块全量源码编译）
- 沙箱 seccomp 拦截 `mq_open` → 内置 LD_PRELOAD 修复库 `libmqsim2.so`（FIFO 模拟 mq）；正常桌面 Linux 无需

## 内置字体（中文字体来源）

镜像内置 **20 个常用中文字体**（宋体/黑体/楷体/仿宋 + 方正系列 + Times New Roman），确保 WPS 转换时中文按公文字体规范渲染。

**打包方式**：字体 `xz -9e` 打包为 `fonts.tar.xz`（~57MB，解压后 ~136MB），构建时 `COPY` 进镜像解压到 `/usr/share/fonts/wps-office/`，无需联网下载。来源：<https://github.com/DoveOutland/Common-Chinese-office-fonts-font-library->（按 GB/T 9704-2012 收集）。**许可提醒**：部分字体仅供非商业用途，商用需自行授权。

### 缺失字体名映射（构建时生成，无需联网）

文档（Google Docs 导出、网页模板、现代 Office）常指定镜像里**不存在的字体名**；WPS 对缺失字体硬编码回落**宋体**（中文），观感差。`fonts_setup.sh` 统一处理：apt 开源字体 + fonts.tar.xz + Noto Sans SC + 别名 conf：

| 文档指定 | 实际渲染 | 说明 |
|---|---|---|
| Noto Sans SC | 思源黑体 SC | Google Fonts 官方 TTF（TrueType），构建时下载；选 TTF 而非 CFF 是关键：**WPS 对 TTF 正常子集化，对 CFF 整字嵌入**（实测 13 页 PPT：TTF 版 PDF 1.4MB，CFF 版 28MB） |
| 微软雅黑 / Microsoft YaHei / 等线 / DengXian | 思源黑体 SC | **版权原因不随镜像分发微软雅黑**，用同设计（黑体）映射 |
| Calibri / Cambria | Carlito / Caladea | apt 安装的度量兼容 OFL 替代（LibreOffice 同款方案） |

> 验证：e2e 测试含"指定微软雅黑 → 嵌入 NotoSansSC 且无 SimSun"断言；`docker run --rm --entrypoint /bin/bash xna00/wps2pdf -c 'fc-match 微软雅黑; fc-match Calibri'` 查看映射结果。

## CI（GitHub Actions）

仓库内置 `.github/workflows/build.yml`，push/PR 到 `main` 时自动：**build**（docker 构建镜像）→ **test**（容器内 e2e 转换，断言 PDF 产物 + 字体映射）→ 带 tag 则 **push-dockerhub**（推 `latest` + 版本号）。

> 仅镜像相关文件变化才触发构建（PR/手动触发无条件）；纯文档改动自动跳过。首次运行约 5–10 分钟（WPS deb ~600MB 拉取 + 构建），后续命中 `type=gha` 缓存显著加快。

### 发布（打 tag）

CI 只监听 main push，一次 push 只跑一次；发布与否由 run 内检测 **HEAD 是否带 `v*` tag** 决定：

```bash
git commit ...
git tag v1.5.0          # 本地先打 tag
git push origin main v1.5.0   # main + tag 显式同推（一次 push = 一次 CI）
```

- 带 `v*` tag → build + test 后**追加发布** Docker Hub（`latest` + 版本号两个 tag）；
- 无 tag → build + test 后结束，不发布；
- **注意**：请用上面**显式列出两个 ref** 的写法。实测 `git push --tags` 只推 tag 不推 main——main 不动则 CI 不触发、发布丢失。也不要"先推 main、稍后补 tag"（补 tag 不会触发 CI）；给旧提交补 tag 同理。

## 目录结构

```
docker/
├── Dockerfile            # 两阶段构建（builder 编译 pywpsrpc + 修复库）
├── Dockerfile.wps12      # WPS 12 备用镜像（12.1.2.28080 + pywpsrpc 2.4.0）
├── entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换
├── http_server.py        # HTTP API 主进程：FastAPI 调度 + 动态 worker 池（MODE=api）
├── rpcengine.py          # WPS 三组件 RPC 引擎：冷启动文件锁 + 实例 PID 认领清理
├── worker.py             # worker 子进程：持有常驻 WPS 实例，循环收任务→转换→回报
├── convert_docx2pdf.py   # CLI 转换脚本（docker run 默认入口）
├── tests/e2e_test.sh     # 端到端测试（CI test job 使用）
├── .github/workflows/    # GitHub Actions：build + test + 发布
├── pywpsrpc-2.4.0-src.tar.gz  # pywpsrpc v2.4.0 源码 + wpsrpc-sdk（2.2MB，主/备用镜像共用）
├── fonts_setup.sh        # 字体统一安装：apt 开源字体 + fonts.tar.xz + Noto Sans SC + 别名 conf
├── fonts.tar.xz          # 常用中文字体包（~57MB）
├── libexitfix.c          # 修复库①：启动器 exit(1)→exit(0)（2.4.0 不再 preload）
├── libmqsim.c            # 修复库②：FIFO 模拟 mq_open/mq_timedreceive
└── pywpsrpc研究报告.md     # 完整逆向与修复链报告
```

## 关键修复点（Dockerfile 内已内置）

| 项目 | 说明 |
|---|---|
| WPS 9662 | RPC server 正常激活的版本（更新的 11723 实测不监听） |
| pywpsrpc v2.4.0 | 配套 `librpc*_sysqt5.so`；sip 6.8.3 + `sip-build` 编译，无需旧 patch |
| `-multiply` | `/usr/bin/wps` 脚本取消注释 `gOptExt=-multiply`（多组件 RPC 激活） |
| 假 gsettings | SDK 的 tablet-mode 探测需成功返回（`/usr/local/bin/gsettings`） |
| setStartTimeout | 单位是**微秒**：`15000000` = 15s |
| API 命名 | pywpsrpc 用 `get_Documents()` / `get_ActiveDocument()` |
| main 入口 | 脚本末尾必须有 `if __name__ == "__main__": main()`；缺失时 Python 退出清理 Qt 直接段错误 |
