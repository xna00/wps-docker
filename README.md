# pywpsrpc RPC 文档转 PDF 镜像（docx / pptx / xlsx / wps / et …）

[![CI](https://github.com/xna00/wps-docker/actions/workflows/build.yml/badge.svg)](https://github.com/xna00/wps-docker/actions)

用 **WPS 11.1.0.9662 + pywpsrpc v2.4.0 原生 RPC** 将 Office/WPS/ODF 等 **16 种格式**文档转为 PDF 的 Docker 方案。内置 20 个中文字体 + 思源黑体（Noto Sans SC），并内置"微软雅黑/等线/Calibri"等缺失字体名 → 思源黑体/Carlito 的映射，支持 CLI 与 HTTP API 两种使用方式。

## 快速开始

### 方式一：CLI（一条命令转换）

```bash
# 输入/输出挂载到 /data，output.pdf 出现在当前目录
docker run --rm -v "$PWD":/data wps2pdf input.docx output.pdf

# 不传参数时默认 /data/input.docx → /data/output.pdf
docker run --rm -v "$PWD":/data wps2pdf
```

支持格式（按扩展名自动路由到 wps/wpp/et 三组件）：

| 组件 | 格式 |
|---|---|
| 文字（wps） | docx doc wps rtf txt xml html htm mht mhtml odt uot uof dot dotx |
| 演示（wpp） | pptx ppt dps pot potx odp uop pps ppsx |
| 表格（et） | xlsx xls et csv ods uos xlt xltx ett prn dif |

### 方式二：HTTP API（常驻服务）

```bash
# 启动（端口默认 8080）
docker run -d --name wps-api -p 8080:8080 -e MODE=api wps2pdf

# 健康检查
curl http://localhost:8080/health
#   {"status":"ok","wps":"ready"}

# 上传文档 → 返回 PDF
curl -o output.pdf -F "file=@input.docx" http://localhost:8080/convert

# 可选环境变量
#   PORT=8080       监听端口
#   MAX_FILE_MB=50  上传大小上限（默认 50MB）
```

> 说明：服务懒初始化 WPS 实例，首次请求约 0.3s，之后**每个请求约 0.13s**（实测 10/10 成功、内存稳定，常驻单例 + 崩溃自动重建）。
> **仅限内网使用，无鉴权**（如需公网暴露请自行加网关鉴权）。

## 构建

```bash
docker build -t wps2pdf .
```

> 所有外部依赖统一走**阿里云镜像**（apt / pip / WPS deb），国内构建快，GitHub 境外 runner 实测同样可达，无需额外参数。
> 说明：
> - WPS deb 单文件约 301MB，超过 GitHub 单文件 100MB 上限，**不能 git commit 进仓库**，构建时从阿里云远程拉取（`curl --retry 5` 带重试）。
> - 若阿里云镜像 URL 失效，可手动下载 `wps-office_11.1.0.9662_amd64.deb` 放本目录，
>   把 Dockerfile 中对应两处 `curl ".../wps-office_11.1.0.9662_amd64.deb"` 改为
>   `COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps.deb`（runtime 阶段）与
>   `COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps-sdk.deb`（builder 阶段）。

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
docker run --rm --entrypoint /bin/bash wps2pdf \
  -c "/opt/wpsrpc-rpc/tests/e2e_test.sh"
```

## 部署指南（生产）

### 方案：单机部署（后端容器 + 可选转发层）

**1) 后端容器（WPS 转换引擎，必装）**

```bash
docker run -d --name wps \
  -p 8080:8080 \
  --memory 2g --memory-swap 2g \
  --restart unless-stopped \
  -e MODE=api \
  xna00/wps2pdf:latest
```

- 接口：`POST /convert`（multipart `file=<文档>` → PDF 二进制）、`GET /health`
- 可选环境变量：`PORT`（默认 8080）、`MAX_FILE_MB`（默认 50）

**2) 转发层（可选：带上传 UI 的网页）**

仓库外另有 `webapp/` 目录（FastAPI 转发层）：提供上传页面并反向代理到后端。

```bash
PORT=3000 UPSTREAM=http://127.0.0.1:8080 python3 forwarder.py   # 生产建议 systemd 托管
```

或只做反向代理：nginx/caddy 把 `/convert`、`/health` 代理到 8080 即可。

**3) 验证**

```bash
curl http://localhost:8080/health                 # {"status":"ok",...}
curl -o out.pdf -F "file=@报告.docx" http://localhost:8080/convert
```

### 生产注意事项

| 事项 | 建议 |
|---|---|
| **内存** | WPS 服务中约 1GB、空闲自动回落 ~360MB；务必加 `--memory 2g` 上限防异常暴涨 |
| **鉴权** | API **无鉴权**，仅限内网；公网暴露必须前置网关鉴权（basic auth / token / 网关白名单） |
| **字体** | 已内置思源黑体（Noto Sans SC TTF）+ 微软雅黑/等线/Calibri 等映射，开箱即用，无需配置 |
| **升级** | `docker pull xna00/wps2pdf:latest` → `docker rm -f wps` → 重新 `docker run`（或 compose `up -d`） |
| **日志** | `docker logs -f wps`；接口错误会打印 `[err]/[warn]`（含 WPS 重建记录） |
| **健康监控** | 定时 `GET /health`，`status:ok` 即存活；单请求超时 180s |

### 发布新版本（CI 自动构建 + 发布 Docker Hub）

```bash
git commit ...
git tag v1.6.0
git push origin main v1.6.0    # 显式同推 main+tag；CI 检测到 v* tag → build+test → 发布 latest+v1.6.0
```

> 注意：不要 `git push --tags`（只推 tag 不推 main，不触发 CI）；也不要发布后立刻推 follow-up 提交（同 ref 的并发取消会杀掉发布 run）。

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

已验证：三组件（wps/wpp/et）RPC 驱动 + 新建→转 PDF 全部通过；共享 `entrypoint.sh`/`http_server.py`/`convert_docx2pdf.py`，API 与主镜像完全兼容。

**修复库说明**：`libmqsim2.so`（seccomp 拦 `mq_open` 的环境修复）对 12 **同样必需**（其 RPC 握手同样走 POSIX 消息队列）；`libexitfix.so`（1.1.0 的启动器退出码补丁）对 pywpsrpc 2.4.0 **已非必需**（实测去掉仍可三组件转换），两个镜像的 entrypoint 均只 preload `libmqsim2.so`，`libexitfix.so` 仅保留文件便于回退验证。

> **注意**：12 为官网 Personal 版，商用需自行确认授权（与 9662 个人版同理）。

## 背景（为什么需要这个组合）

- WPS 11.1.0.11723+ 的 RPC server 不 LISTEN；**11.1.0.9662（2020 年）RPC server 正常**
- 关于 WPS 12：官网 **12.1.2.28080 实测可用**——RPC SDK 库从 `librpc*_sysqt5.so` 改名 `librpc*_wpsqt.so`，需配套 pywpsrpc 2.4.0（见上文「WPS 12 备用镜像」）；早期社区"12 自动化接口失效"的说法与库名/版本不匹配有关
- 9662 的客户端库是 `librpcwpsapi_sysqt5.so`（另含 `librpcwppapi_sysqt5.so` / `librpcetapi_sysqt5.so`），配套 **pywpsrpc v2.4.0**（2.4.0 的 `project.py` 按 `["wpsqt","sysqt5"]` 顺序探测，自动链接 9662 的 `_sysqt5`；本镜像已源码编译，wps/wpp/et 三模块全量编译）
- 沙箱 seccomp 拦截 `mq_open(O_CREAT)`、无 UKUI 桌面 → 本镜像内置 LD_PRELOAD 修复库 `libmqsim2.so`（FIFO 模拟 mq）
- 正常桌面 Linux + WPS 11.1.0.9662 上**无需**该修复库，版本匹配 + `-multiply` 即可

## 内置字体（中文字体来源）

镜像内置 **20 个常用中文字体**（宋体/黑体/楷体/仿宋 + 方正系列 + Times New Roman），确保 WPS 转换时中文按公文字体规范渲染。

**来源仓库**：<https://github.com/DoveOutland/Common-Chinese-office-fonts-font-library->

- 该仓库按《党政机关公文格式》（GB/T 9704-2012）收集常用中文字体，含中易（宋体/黑体/楷体/仿宋）、方正（大标宋/小标宋/仿宋/楷体/黑体）、长城（楷体_GB2312）等
- **许可提醒**：部分字体仅供**非商业用途**，商用需自行联系字体厂商授权（详见该仓库 README）

**打包方式**：20 个字体文件 `xz -9e` 打包为 `fonts.tar.xz`（约 57MB，解压后约 136MB），随仓库分发，构建时 `COPY` 进镜像解压到 `/usr/share/fonts/wps-office/`，无需联网下载。

**字体清单**：

| 类别 | 字体文件 |
|---|---|
| 中易 | 宋体.ttc、黑体.ttf、楷体.ttf、仿宋.ttf |
| 中易 GB2312 | 仿宋_GB2312.ttf、楷体_GB2312.ttf |
| 方正 | 方正仿宋_GBK.ttf、方正仿宋简体.ttf、方正大标宋简体.ttf、方正大标宋简繁.ttf、方正小标宋_GBK.ttf、方正小标宋简体.ttf、方正楷体_GBK.ttf、方正楷体简体.ttf、方正黑体_GBK.ttf、方正黑体简体.ttf |
| 西文 | Times New Roman/times.ttf、timesbd.ttf、timesbi.ttf、timesi.ttf |

### 缺失字体名映射（构建时生成，无需联网）

文档（尤其 Google Docs 导出、网页模板、现代 Office）常指定镜像里**不存在的字体名**；WPS 对缺失字体硬编码回落**宋体**（中文）/ URW Bookman（西文），观感差。**所有字体逻辑集中在 `fonts_setup.sh` 一个脚本**（apt 开源字体 + fonts.tar.xz + 思源黑体 SC 生成 + 别名 conf），构建时执行一次，核心两层处理：

1. **Noto Sans SC 官方 TTF**：构建时从 Google Fonts 下载官方静态 TrueType 字体（`fonts.gstatic.com` 国内直连可用，CSS 解析失败时回退 loli 镜像），家族名天生即 "Noto Sans SC"，无需转换/改名。选 TrueType 而非 CFF 是关键：**WPS 对 TrueType 正常子集化，对 CFF 整字嵌入**（实测 13 页 PPT：TTF 版 PDF 仅 1.4MB，CFF 版膨胀到 28MB）。
2. **fontconfig scan 别名**（写入 `/etc/fonts/conf.d/99-font-aliases.conf`）：把常见缺失名字在扫描阶段追加为真实家族名（WPS 只认字体枚举里出现的名字，不认 pattern 别名）：

| 文档指定 | 实际渲染 | 说明 |
|---|---|---|
| Noto Sans SC | 思源黑体 SC | Google Fonts 官方 TTF（TrueType，构建时下载） |
| 微软雅黑 / Microsoft YaHei / Microsoft YaHei UI | 思源黑体 SC | **版权原因不随镜像分发微软雅黑**，用同设计（黑体）映射 |
| 等线 / DengXian | 思源黑体 SC | Win10/11 Office 中文默认字体 |
| Noto Sans | 思源黑体 SC | 纯西文名，思源含完整 Latin 字形 |
| Calibri / Cambria | Carlito / Caladea | apt 安装的度量兼容 OFL 替代（LibreOffice 同款方案） |

> 验证：e2e 测试含"指定微软雅黑 → 嵌入 NotoSansSC 且无 SimSun"断言；也可 `docker run --rm --entrypoint /bin/bash wps2pdf -c 'fc-match 微软雅黑; fc-match Calibri'` 查看映射结果。

**如何更新字体**：上游仓库有更新时，重新下载字体 → `tar -cf - 字体目录 | xz -9e > fonts.tar.xz` → 替换本目录文件 → 提交即可。新增缺失字体名映射只需编辑 `fonts_setup.sh` 中的别名 conf 段（第 [4/5] 步 heredoc）。

## CI（GitHub Actions）

仓库内置 `.github/workflows/build.yml`，push/PR 到 `main` 时自动：

1. **build job**：`docker/build-push-action` 构建镜像（WPS deb 走仓库 Release 附件 CDN，apt/pip 走阿里云），带 `type=gha` 构建缓存
2. **test job**：`docker run --entrypoint /bin/bash ... e2e_test.sh` 在容器内真跑一次 docx→pdf 转换，断言 PDF 产物存在、>1KB、`%PDF-` 头、页数 ≥ 1

> 仅镜像相关文件变化才触发构建（PR/手动触发无条件）；纯文档改动自动跳过。首次运行约 5–10 分钟（WPS deb ~600MB 拉取 + 构建）；后续命中缓存会显著加快。

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
├── download_wps12.sh     # WPS 12 deb 官网动态签名下载脚本
├── entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换
├── convert_docx2pdf.py   # RPC 转换脚本（getWpsApplication → Open → SaveAs2 PDF）
├── http_server.py        # HTTP 服务（MODE=api：POST /convert → PDF）
├── tests/e2e_test.sh     # 端到端测试（CI test job 使用）
├── .github/workflows/    # GitHub Actions：build + test
├── pywpsrpc-2.4.0-src.tar.gz  # pywpsrpc v2.4.0 源码 + wpsrpc-sdk（2.2MB，主/备用镜像共用）
├── fonts_setup.sh        # 字体统一安装脚本：apt 开源字体 + fonts.tar.xz + Noto Sans SC 生成 + 别名 conf
├── fonts.tar.xz          # 常用中文字体包（20 个字体，xz -9e，~57MB）
├── libexitfix.c          # 修复库①：启动器 exit(1)→exit(0)（源码，2.4.0 不再 preload）
├── libmqsim.c            # 修复库②：FIFO 模拟 mq_open/mq_timedreceive（源码）
└── pywpsrpc研究报告.md     # 完整逆向与修复链报告
```

## 关键修复点（Dockerfile 内已内置）

| 项目 | 说明 |
|---|---|
| WPS 9662 | RPC server 正常激活的版本（更新的 11.1.0.11723 实测 RPC server 不监听） |
| pywpsrpc v2.4.0 | 配套 `librpcwpsapi_sysqt5.so`（project.py 自动探测）；sip 6.8.3 + `sip-build` 编译，无需 sip5.5/siplib patch |
| `-multiply` | `/usr/bin/wps` 脚本取消注释 `gOptExt=-multiply`（多组件 RPC 激活） |
| wps 脚本 exec | 默认分支改 `exec` 直接启动（避免 bash 中间层退出误判） |
| 假 gsettings | SDK 的 tablet-mode 探测需成功返回（`/usr/local/bin/gsettings`） |
| libexitfix.so | 启动器进程 `exit(1)→exit(0)`（1.1.0 时代的补丁；2.4.0 实测不需要，仅保留文件备回退） |
| libmqsim.so | 容器 seccomp 拦截 `mq_open(O_CREAT)` → 用 FIFO+poll 模拟全套 mq 接口 |
| setStartTimeout | 单位是**微秒**：`15000000` = 15s |
| API 命名 | pywpsrpc 用 `get_Documents()` / `get_ActiveDocument()`（1.1.0/2.4.0 一致） |
| DBUS 地址 | 转换脚本用 `setdefault` 沿用 entrypoint 里 `dbus-launch` 注入的真实会话地址，不硬编码覆盖 |
| main 入口 | 脚本末尾必须有 `if __name__ == "__main__": main()`；缺失时 `main()` 不执行、Python 退出清理 Qt 直接段错误 |
