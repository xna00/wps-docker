# pywpsrpc RPC docx→pdf 转换镜像

用 **WPS 11.1.0.9662 + pywpsrpc v1.1.0 原生 RPC** 将 docx 转为 PDF 的 Docker 方案。

## 背景（为什么需要这个组合）

- WPS 12.1.0.225xx 之后 Linux 自动化接口（COM/RPC）普遍失效（官方论坛 bbs.wps.cn/topic/62639 佐证）
- WPS 11.1.0.11723+ 的 RPC server 不 LISTEN；**11.1.0.9662（2020 年）RPC server 正常**
- 9662 的客户端库是 `librpcwpsapi_sysqt5.so`，需配套 **pywpsrpc v1.1.0**（本镜像已源码编译）
- 沙箱 seccomp 拦截 `mq_open(O_CREAT)`、无 UKUI 桌面 → 本镜像内置两个 LD_PRELOAD 修复库
- 正常桌面 Linux + WPS 11.1.0.9662 上**无需**这两个修复库，版本匹配 + `-multiply` 即可

## 构建

```bash
docker build -t wps-docx2pdf .
```

> 所有外部依赖统一走**阿里云镜像**（apt / pip / WPS deb），国内构建快，GitHub 境外 runner 实测同样可达，无需任何 build-arg。
> 说明：
> - WPS deb 单文件约 301MB，超过 GitHub 单文件 100MB 上限，**不能 git commit 进仓库**，构建时从阿里云远程拉取（`curl --retry 5` 带重试）。
> - 若阿里云镜像 URL 失效，可手动下载 `wps-office_11.1.0.9662_amd64.deb` 放本目录，
>   把 Dockerfile 中对应两处 `curl ".../wps-office_11.1.0.9662_amd64.deb"` 改为
>   `COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps.deb`（runtime 阶段）与
>   `COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps-sdk.deb`（builder 阶段）。

## 使用

```bash
# 基本用法：输入/输出挂载到 /data
docker run --rm -v "$PWD":/data wps-docx2pdf input.docx output.pdf

# 默认路径（不传参数）
docker run --rm -v "$PWD":/data wps-docx2pdf
#   等价于: wps-docx2pdf /data/input.docx /data/output.pdf
```

转换完成后，`output.pdf` 出现在挂载目录。

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

1. **build job**：`docker/build-push-action` 构建镜像（统一走阿里云源，国内/境外均可用），带 `type=gha` 构建缓存
2. **test job**：`docker run --entrypoint /bin/bash ... e2e_test.sh` 在容器内真跑一次 docx→pdf 转换，断言 PDF 产物存在、>1KB、`%PDF-` 头、页数 ≥ 1

> 首次运行约 5–10 分钟（WPS deb ~600MB 拉取 + 构建）；后续命中缓存会显著加快。

## 目录结构

```
docker/
├── Dockerfile            # 两阶段构建（builder 编译 pywpsrpc + 修复库）
├── entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换
├── convert_docx2pdf.py   # RPC 转换脚本（getWpsApplication → Open → SaveAs2 PDF）
├── tests/e2e_test.sh     # 端到端测试（CI test job 使用）
├── .github/workflows/    # GitHub Actions：build + test
├── pywpsrpc-src.tar.gz   # pywpsrpc v1.1.0 源码 + wpsrpc-sdk（11MB）
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
