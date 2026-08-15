# pywpsrpc RPC 文档转 PDF 镜像（docx / pptx / xlsx / wps / et …）

用 **WPS 11.1.0.9662 + pywpsrpc v1.1.0 原生 RPC** 将 Office/WPS/ODF 等格式文档转为 PDF 的 Docker 方案。

## 背景（为什么需要这个组合）

- WPS 12.1.0.225xx 之后 Linux 自动化接口（COM/RPC）普遍失效（官方论坛 bbs.wps.cn/topic/62639 佐证）
- WPS 11.1.0.11723+ 的 RPC server 不 LISTEN；**11.1.0.9662（2020 年）RPC server 正常**
- 9662 的客户端库是 `librpcwpsapi_sysqt5.so`（另含 `librpcwppapi_sysqt5.so` / `librpcetapi_sysqt5.so`），需配套 **pywpsrpc v1.1.0**（本镜像已源码编译，wps/wpp/et 三模块全量编译）
- 沙箱 seccomp 拦截 `mq_open(O_CREAT)`、无 UKUI 桌面 → 本镜像内置两个 LD_PRELOAD 修复库
- 正常桌面 Linux + WPS 11.1.0.9662 上**无需**这两个修复库，版本匹配 + `-multiply` 即可

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

**如何更新字体**：上游仓库有更新时，重新下载字体 → `tar -cf - 字体目录 | xz -9e > fonts.tar.xz` → 替换本目录文件 → 提交即可。

## 构建

```bash
docker build -t wps-docx2pdf .
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

## 使用

```bash
# 基本用法：输入/输出挂载到 /data
docker run --rm -v "$PWD":/data wps-docx2pdf input.docx output.pdf

# 默认路径（不传参数）
docker run --rm -v "$PWD":/data wps-docx2pdf
#   等价于: wps-docx2pdf /data/input.docx /data/output.pdf
```

转换完成后，`output.pdf` 出现在挂载目录。

## HTTP 服务（API 模式）

常驻 WPS 实例的 HTTP 转换服务，**仅限内网使用，无鉴权**（如需公网暴露请自行加网关鉴权）。

```bash
# 启动（端口默认 8080）
docker run -d --name wps-api -p 8080:8080 \
  -e MODE=api \
  wps-docx2pdf

# 健康检查
curl http://localhost:8080/health
#   {"status":"ok","wps":"ready"}

# 上传 docx → 返回 PDF
curl -o output.pdf -F "file=@input.docx" http://localhost:8080/convert

# 可选环境变量
#   PORT=8080       监听端口
#   MAX_FILE_MB=50  上传大小上限（默认 50MB）
```

> 说明：服务启动时懒初始化 WPS 实例，首次请求约 0.3s，之后**每个请求约 0.13s**（实测 10/10 成功、内存稳定，常驻单例 + 崩溃自动重建）。

## 验证

```bash
pdfinfo output.pdf        # Creator 应为 "WPS 文字"，Pages 2
pdftotext output.pdf -    # 中文内容完整

# 端到端自动化测试（生成最小 docx → RPC 转换 → 断言 PDF 产物/页数）
docker run --rm --entrypoint /bin/bash wps-docx2pdf \
  -c "/opt/wpsrpc-rpc/tests/e2e_test.sh"
```

## CI（GitHub Actions）

仓库内置 `.github/workflows/build.yml`，push/PR 到 `main` 时自动：

1. **build job**：`docker/build-push-action` 构建镜像（WPS deb 走仓库 Release 附件 CDN，apt/pip 走阿里云），带 `type=gha` 构建缓存
2. **test job**：`docker run --entrypoint /bin/bash ... e2e_test.sh` 在容器内真跑一次 docx→pdf 转换，断言 PDF 产物存在、>1KB、`%PDF-` 头、页数 ≥ 1

> 首次运行约 5–10 分钟（WPS deb ~600MB 拉取 + 构建）；后续命中缓存会显著加快。

## 目录结构

```
docker/
├── Dockerfile            # 两阶段构建（builder 编译 pywpsrpc + 修复库）
├── entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换
├── convert_docx2pdf.py   # RPC 转换脚本（getWpsApplication → Open → SaveAs2 PDF）
├── http_server.py        # HTTP 服务（MODE=api：POST /convert → PDF）
├── tests/e2e_test.sh     # 端到端测试（CI test job 使用）
├── .github/workflows/    # GitHub Actions：build + test
├── pywpsrpc-src.tar.gz   # pywpsrpc v1.1.0 源码 + wpsrpc-sdk（11MB）
├── fonts.tar.xz          # 常用中文字体包（20 个字体，xz -9e，~57MB）
├── libexitfix.c          # 修复库①：启动器 exit(1)→exit(0)（源码）
├── libmqsim.c            # 修复库②：FIFO 模拟 mq_open/mq_timedreceive（源码）
└── pywpsrpc研究报告.md     # 完整逆向与修复链报告
```

## 关键修复点（Dockerfile 内已内置）

| 项目 | 说明 |
|---|---|
| WPS 9662 | RPC server 正常激活的版本（12.x 自动化接口失效） |
| pywpsrpc v1.1.0 | 配套 `librpcwpsapi_sysqt5.so`；sip 5.5.0 已 patch 支持 Py3.12，siplib.c 已改用 `PyFrame_GetBack` |
| `-multiply` | `/usr/bin/wps` 脚本取消注释 `gOptExt=-multiply`（多组件 RPC 激活） |
| wps 脚本 exec | 默认分支改 `exec` 直接启动（避免 bash 中间层退出误判） |
| 假 gsettings | SDK 的 tablet-mode 探测需成功返回（`/usr/local/bin/gsettings`） |
| libexitfix.so | 启动器进程 `exit(1)→exit(0)`（客户端将非 0 退出码判为失败） |
| libmqsim.so | 容器 seccomp 拦截 `mq_open(O_CREAT)` → 用 FIFO+poll 模拟全套 mq 接口 |
| setStartTimeout | 单位是**微秒**：`15000000` = 15s |
| API 命名 | v1.1.0 用 `get_Documents()` / `get_ActiveDocument()` |
| DBUS 地址 | 转换脚本用 `setdefault` 沿用 entrypoint 里 `dbus-launch` 注入的真实会话地址，不硬编码覆盖 |
| main 入口 | 脚本末尾必须有 `if __name__ == "__main__": main()`；缺失时 `main()` 不执行、Python 退出清理 Qt 直接段错误 |
