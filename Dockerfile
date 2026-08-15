# syntax=docker/dockerfile:1
# ======================================================================
# pywpsrpc RPC docx→pdf 转换镜像
#   方案：WPS 11.1.0.9662（Qt4 时代，RPC server 正常）+
#         pywpsrpc v2.4.0（源码编译，自动探测并链接 librpcwpsapi_sysqt5.so）
#   构建：docker build -t wps2pdf .
#   使用：docker run --rm -v $PWD:/data wps2pdf input.docx output.pdf
#   （输入/输出默认 /data/input.docx → /data/output.pdf）
# ======================================================================

# ----------------------------------------------------------------------
# 阶段 1：编译 pywpsrpc v2.4.0 + 两个 LD_PRELOAD 修复库
# ----------------------------------------------------------------------
FROM ubuntu:24.04 AS builder

# WPS deb 完整 URL。默认阿里云（国内构建）；GitHub CI 可用 --build-arg 覆盖为 Release 附件
ARG WPS_DEB_URL="https://mirrors.aliyun.com/ubuntukylin/pool/partner/wps-office_11.1.0.9662_amd64.deb"

ENV DEBIAN_FRONTEND=noninteractive

# 统一走阿里云镜像源（国内构建快，GitHub 境外 runner 实测也可达）
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential python3-dev python3-pip qt5-qmake qtbase5-dev \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# pip 用阿里云源（Ubuntu 24.04 PEP668 保护，用 --break-system-packages）
RUN pip3 config set global.index-url https://mirrors.aliyun.com/pypi/simple/

# sip 6.8.3（pywpsrpc 2.4.0 兼容区间：sip 6.15+ 改 API 编译崩、sip 6.5.x 不支持 Py3.12 ABI）
RUN pip3 install --no-cache-dir --break-system-packages sip==6.8.3

# --- pywpsrpc v2.4.0 源码（含 wpsrpc-sdk 头文件，打包自 timxx/pywpsrpc v2.4.0）---
COPY pywpsrpc-2.4.0-src.tar.gz /tmp/
RUN cd /tmp && tar xzf pywpsrpc-2.4.0-src.tar.gz && mv pywpsrpc-pack pywpsrpc-full

# --- WPS SDK 库：librpcwpsapi_sysqt5.so（仅提取，不完整安装）---
# 注：WPS deb 单文件 ~301MB，超过 GitHub 单文件 100MB 上限，不能 git commit 进仓库，只能远程拉取。
# pywpsrpc 2.4.0 的 project.py 按 ["wpsqt","sysqt5"] 顺序探测，9662 的 office6 只有 _sysqt5 → 自动链接它
RUN curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/wps-sdk.deb \
        "${WPS_DEB_URL}" \
    && dpkg-deb -x /tmp/wps-sdk.deb /tmp/wps-x \
    && mkdir -p /opt/kingsoft/wps-office/office6 \
    && cp /tmp/wps-x/opt/kingsoft/wps-office/office6/librpcwpsapi_sysqt5.so \
          /tmp/wps-x/opt/kingsoft/wps-office/office6/librpcwppapi_sysqt5.so \
          /tmp/wps-x/opt/kingsoft/wps-office/office6/librpcetapi_sysqt5.so \
          /opt/kingsoft/wps-office/office6/ \
    && ls -la /opt/kingsoft/wps-office/office6/librpc*sysqt5.so \
    && rm -f /tmp/wps-sdk.deb && rm -rf /tmp/wps-x

# --- 编译 pywpsrpc v2.4.0（sip-build 一条命令；2.4.0 无需 sip5.5/siplib 的 Python 3.12 patch）---
WORKDIR /tmp/pywpsrpc-full
RUN sip-build 2>&1 | tail -5

# --- 安装到 Python 3.12 的 dist-packages ---
RUN SP=$(python3 -c 'import site; print(site.getsitepackages()[0])') \
    && mkdir -p $SP/pywpsrpc \
    && cp -r build/pywpsrpc/* $SP/pywpsrpc/ \
    && ls -la $SP/pywpsrpc/ \
    && ldd $SP/pywpsrpc/rpcwpsapi.so | grep librpc

# --- 编译两个 LD_PRELOAD 修复库 ---
# 注：libexitfix 是 pywpsrpc 1.1.0 时代的启动器退出码补丁，2.4.0 已不需要（实测 9662/12 均无需），
#     仅保留文件便于回退验证；entrypoint 实际只 preload libmqsim2.so。
COPY libexitfix.c libmqsim.c /tmp/
RUN gcc -shared -fPIC -O2 -o /tmp/libexitfix.so /tmp/libexitfix.c \
    && gcc -shared -fPIC -O2 -o /tmp/libmqsim2.so /tmp/libmqsim.c -lrt \
    && ls -la /tmp/libexitfix.so /tmp/libmqsim2.so

# ----------------------------------------------------------------------
# 阶段 2：运行时镜像
# ----------------------------------------------------------------------
FROM ubuntu:24.04

# runtime 阶段需重新声明 ARG（每个 FROM 都会重置构建参数作用域）
ARG WPS_DEB_URL="https://mirrors.aliyun.com/ubuntukylin/pool/partner/wps-office_11.1.0.9662_amd64.deb"
# 中文字体包（fonts.tar.xz，~57MB）：默认走仓库 Release 附件，可覆盖为其他镜像
ARG FONTS_URL="https://github.com/xna00/wps-docker/releases/download/wps-11.1.0.9662/fonts.tar.xz"

ENV DEBIAN_FRONTEND=noninteractive

# 统一走阿里云镜像源（国内构建快，GitHub 境外 runner 实测也可达）
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true

# --- 运行时依赖（Xvfb / Qt5 / 字体 / WPS 9662 的旧依赖）---
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils xdg-utils dbus dbus-x11 fluxbox \
        libqt5gui5 libqt5widgets5 libqt5core5a libqt5x11extras5 \
        libqt5dbus5 libqt5network5 libqt5printsupport5 libqt5svg5 libqt5xml5 \
        libglu1-mesa libgl1 libasound2t64 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
        libxcb-xkb1 libxkbcommon-x11-0 libxcomposite1 libxcursor1 libxrandr2 \
        libxi6 libxtst6 libnss3 libgbm1 libxslt1.1 libsm6 libcups2 \
        libxrender1 libxext6 libbz2-1.0 libfreetype6 libglib2.0-0 \
        libtiff6 fontconfig fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk \
        poppler-utils python3 python3-pip ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- HTTP 服务依赖（FastAPI + uvicorn）---
RUN pip3 install --no-cache-dir --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/ \
        fastapi "uvicorn[standard]" python-multipart

# --- WPS Office 11.1.0.9662（默认阿里云 ubuntukylin，CI 可覆盖为 GitHub Release 附件，~301MB）---
# 若 URL 失效，可手动下载 wps-office_11.1.0.9662_amd64.deb 放到构建目录，
# 并改用: COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps.deb
RUN curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/wps.deb \
        "${WPS_DEB_URL}" \
    && dpkg -i /tmp/wps.deb || apt-get -f install -y \
    && rm -f /tmp/wps.deb \
    && dpkg -l | grep wps-office

# --- libtiff 兼容（WPS 9662 需要 libtiff.so.5）---
RUN ln -sf /usr/lib/x86_64-linux-gnu/libtiff.so.6 /usr/lib/x86_64-linux-gnu/libtiff.so.5

# --- 常用中文字体（宋体/黑体/楷体/仿宋 + 方正系列 + Times New Roman）---
# 来源: https://github.com/DoveOutland/Common-Chinese-office-fonts-font-library-
# 打包为 fonts.tar.xz 随仓库 Release 分发（~57MB，解压后 ~136MB），构建时下载
# 字体仅供非商业用途，商用需自行授权（详见该仓库 README）
RUN curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/fonts.tar.xz \
        "${FONTS_URL}" \
    && mkdir -p /usr/share/fonts/wps-office \
    && python3 -c "import tarfile; tarfile.open('/tmp/fonts.tar.xz').extractall('/usr/share/fonts/wps-office/')" \
    && rm -f /tmp/fonts.tar.xz \
    && fc-cache -f >/dev/null 2>&1 \
    && fc-list | grep -cE "宋体|黑体|楷体|仿宋|Times" | xargs echo "内置字体族数:"

# --- 屏蔽 WPS 更新服务器 ---
RUN sed -i 's|Address0=.*|Address0=http://127.0.0.1/blocked|' \
        /opt/kingsoft/wps-office/office6/cfgs/setup.cfg

# --- Office.conf：接受 EULA + 多组件模式 + 关闭首次安装/自动重启 ---
RUN mkdir -p /root/.config/Kingsoft && cat > /root/.config/Kingsoft/Office.conf <<'EOF'
[General]
AcceptedEULA=true
languages=zh_CN
FirstRun=false
UpdateMode=manual
CloudServiceEnabled=false

[6.0]
common\AcceptedEULA=true
common\newInstall=false
common\DefaultLanguage=2052
common\Local\UILanguage=2052
common\PromeWindowCount=40
common\loginSafeVersion=true
wpsoffice\Application%20Settings\AppComponentMode=prome_independ
wpsoffice\Application%20Settings\AppComponentModeInstall=prome_independ
EOF

# --- /usr/bin/wps 脚本修复：启用 -multiply（多组件 RPC 激活）+ exec 直接启动 ---
RUN sed -i 's|#gOptExt=-multiply|gOptExt=-multiply|' /usr/bin/wps \
    && python3 - <<'PYEOF'
p = '/usr/bin/wps'
src = open(p).read()
old = '''		else
			{ ${gInstallPath}/office6/${gApp}  ${gOptExt} ${gOpt} "$@"; } > /dev/null 2>&1
		fi'''
new = '''		else
			exec ${gInstallPath}/office6/${gApp}  ${gOptExt} ${gOpt} "$@" > /dev/null 2>&1
		fi'''
assert old in src, 'wps script pattern not found'
open(p, 'w').write(src.replace(old, new))
print('wps script patched')
PYEOF

# --- 假 gsettings（SDK 的 tablet-mode 探测需要成功返回）---
RUN cat > /usr/local/bin/gsettings <<'EOF'
#!/bin/bash
if [ "$1" = "get" ]; then echo "false"; exit 0; fi
if [ "$1" = "set" ]; then exit 0; fi
exit 0
EOF
RUN chmod +x /usr/local/bin/gsettings

# --- 拷贝 pywpsrpc + 修复库 + 转换脚本 ---
COPY --from=builder /usr/local/lib/python3.12/dist-packages/pywpsrpc /usr/local/lib/python3.12/dist-packages/pywpsrpc
COPY --from=builder /tmp/libexitfix.so /tmp/libmqsim2.so /opt/wpsrpc-fix/
COPY convert_docx2pdf.py entrypoint.sh http_server.py /opt/wpsrpc-rpc/
COPY tests/e2e_test.sh /opt/wpsrpc-rpc/tests/e2e_test.sh
RUN chmod +x /opt/wpsrpc-rpc/entrypoint.sh /opt/wpsrpc-rpc/tests/e2e_test.sh

# --- 默认工作区 ---
RUN mkdir -p /data
WORKDIR /data

# HTTP 服务端口（MODE=api 时使用）
EXPOSE 8080

ENTRYPOINT ["/opt/wpsrpc-rpc/entrypoint.sh"]
