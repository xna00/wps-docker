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
cd docker
docker build -t wps-docx2pdf .
```

> 构建时会从阿里云镜像下载 301MB 的 WPS deb（URL 失效可手动下载
> `wps-office_11.1.0.9662_amd64.deb` 放本目录，把 Dockerfile 中 `ADD https://...` 改为
> `COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps.deb`）。

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
```

## 目录结构

```
docker/
├── Dockerfile            # 两阶段构建（builder 编译 pywpsrpc + 修复库）
├── entrypoint.sh         # 容器入口：Xvfb + dbus + fluxbox + 转换
├── convert_docx2pdf.py   # RPC 转换脚本（getWpsApplication → Open → SaveAs2 PDF）
├── pywpsrpc-src.tar.gz   # pywpsrpc v1.1.0 源码 + wpsrpc-sdk（11MB）
├── libexitfix.c          # 修复库①：启动器 exit(1)→exit(0)（源码）
└── libmqsim.c            # 修复库②：FIFO 模拟 mq_open/mq_timedreceive（源码）
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
