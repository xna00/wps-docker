# syntax=docker/dockerfile:1
# ======================================================================
# pywpsrpc RPC docx→pdf 转换镜像
#   方案：WPS 11.1.0.9662（Qt4 时代，RPC server 正常）+
#         pywpsrpc v1.1.0（源码编译，配套 librpcwpsapi_sysqt5.so）
#   构建：docker build -t wps-docx2pdf .
#   使用：docker run --rm -v $PWD:/data wps-docx2pdf input.docx output.pdf
#   （输入/输出默认 /data/input.docx → /data/output.pdf）
# ======================================================================

# ----------------------------------------------------------------------
# 阶段 1：编译 pywpsrpc v1.1.0 + 两个 LD_PRELOAD 修复库
# ----------------------------------------------------------------------
FROM ubuntu:24.04 AS builder

ARG APT_MIRROR_BASE="http://archive.ubuntu.com/ubuntu"
ARG PIP_INDEX="https://pypi.org/simple"
ENV DEBIAN_FRONTEND=noninteractive

# 默认 Ubuntu 官方源（适用于 GitHub Actions 等境外 runner）；
# 国内/内网构建可传: --build-arg APT_MIRROR_BASE=http://mirrors.aliyun.com/ubuntu
RUN sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR_BASE}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR_BASE}|g" \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential python3-dev python3-pip qt5-qmake qtbase5-dev \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# pip 索引源可覆盖（默认官方；国内构建传 --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/）
RUN pip3 config set global.index-url ${PIP_INDEX}

# sip 5.5.0（最后一个 sip5，官方只支持到 Py3.9，需 patch 支持 3.12）
RUN pip3 install --no-cache-dir --break-system-packages sip==5.5.0

# --- patch sip 5.5.0 支持 Python 3.12 ---
RUN python3 - <<'PYEOF'
import sipbuild, os
p = os.path.join(os.path.dirname(sipbuild.__file__), 'py_versions.py')
src = open(p).read()
src = src.replace('LAST_SUPPORTED_MINOR = 9', 'LAST_SUPPORTED_MINOR = 12')
open(p, 'w').write(src)
print('patched:', p)
PYEOF

# --- pywpsrpc v1.1.0 源码（含 wpsrpc-sdk）---
COPY pywpsrpc-src.tar.gz /tmp/
RUN cd /tmp && tar xzf pywpsrpc-src.tar.gz

# --- WPS SDK 库：链接 pywpsrpc 需要 librpcwpsapi_sysqt5.so（仅提取，不完整安装）---
# WPS deb 默认走阿里云 ubuntukylin 镜像（已验证 200）；可用 --build-arg WPS_DEB_BASE 覆盖为其他镜像。
# 注：WPS deb 单文件 ~301MB，超过 GitHub 单文件 100MB 上限，不能 git commit 进仓库，只能远程拉取。
ARG WPS_DEB_BASE="https://mirrors.aliyun.com/ubuntukylin/pool/partner"
RUN curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/wps-sdk.deb \
        "${WPS_DEB_BASE}/wps-office_11.1.0.9662_amd64.deb" \
    && dpkg-deb -x /tmp/wps-sdk.deb /tmp/wps-x \
    && mkdir -p /opt/kingsoft/wps-office/office6 \
    && cp /tmp/wps-x/opt/kingsoft/wps-office/office6/librpcwpsapi_sysqt5.so /opt/kingsoft/wps-office/office6/ \
    && ls -la /opt/kingsoft/wps-office/office6/librpcwpsapi_sysqt5.so \
    && rm -f /tmp/wps-sdk.deb && rm -rf /tmp/wps-x

# --- 生成 sip 项目（编译失败可忽略，产物用于后续 patch + 分模块编译）---
WORKDIR /tmp/pywpsrpc-full
RUN cd /tmp/pywpsrpc-full \
    && sip-build 2>&1 | tail -5 || true

# --- patch siplib.c（Python 3.12 移除了公开的 _frame 结构）---
RUN python3 - <<'PYEOF'
p = '/tmp/pywpsrpc-full/build/sip/siplib.c'
src = open(p).read()
start = src.index('static struct _frame *sip_api_get_frame(int depth)')
end = src.index('/*\n * Check if a type was generated using the given plugin.')
new_fn = '''static struct _frame *sip_api_get_frame(int depth)
{
#if defined(PYPY_VERSION)
    /* PyPy only supports a depth of 0. */
    return NULL;
#else
    PyFrameObject *frame = PyEval_GetFrame();  /* borrowed */
    Py_XINCREF(frame);

    while (frame != NULL && depth > 0)
    {
        PyFrameObject *back = PyFrame_GetBack(frame);  /* new ref or NULL */
        Py_DECREF(frame);
        frame = back;
        --depth;
    }

    return (struct _frame *)frame;
#endif
}


'''
src = src[:start] + new_fn + src[end:]
open(p, 'w').write(src)
print('siplib.c patched')
PYEOF

# --- 分模块编译：common / rpcwpsapi / sip（跳过会报错的 rpcwppapi、rpcetapi）---
RUN cd /tmp/pywpsrpc-full/build/common && make -j$(nproc) 2>&1 | tail -2
RUN cd /tmp/pywpsrpc-full/build/rpcwpsapi && make -j$(nproc) 2>&1 | grep -E "undefined reference|cannot find" | head -15
RUN cd /tmp/pywpsrpc-full/build/sip && make -j$(nproc) 2>&1 | tail -2
RUN ls -la /tmp/pywpsrpc-full/build/sip/sip.so \
        /tmp/pywpsrpc-full/build/common/common.so \
        /tmp/pywpsrpc-full/build/rpcwpsapi/rpcwpsapi.so

# --- 安装到 Python 3.12 的 dist-packages（从子目录产物拷贝）---
RUN SP=$(python3 -c 'import site; print(site.getsitepackages()[0])') \
    && mkdir -p $SP/pywpsrpc \
    && cp /tmp/pywpsrpc-full/py/__init__.py $SP/pywpsrpc/ \
    && cp /tmp/pywpsrpc-full/py/utils.py $SP/pywpsrpc/ \
    && cp /tmp/pywpsrpc-full/build/common/common.so $SP/pywpsrpc/ \
    && cp /tmp/pywpsrpc-full/build/rpcwpsapi/rpcwpsapi.so $SP/pywpsrpc/ \
    && cp /tmp/pywpsrpc-full/build/sip/sip.so $SP/pywpsrpc/ \
    && ls -la $SP/pywpsrpc/

# --- 编译两个 LD_PRELOAD 修复库 ---
COPY libexitfix.c libmqsim.c /tmp/
RUN gcc -shared -fPIC -O2 -o /tmp/libexitfix.so /tmp/libexitfix.c \
    && gcc -shared -fPIC -O2 -o /tmp/libmqsim2.so /tmp/libmqsim.c -lrt \
    && ls -la /tmp/libexitfix.so /tmp/libmqsim2.so

# ----------------------------------------------------------------------
# 阶段 2：运行时镜像
# ----------------------------------------------------------------------
FROM ubuntu:24.04

# runtime 阶段需重新声明 ARG（每个 FROM 都会重置构建参数作用域）
ARG APT_MIRROR_BASE="http://archive.ubuntu.com/ubuntu"
ARG WPS_DEB_BASE="https://mirrors.aliyun.com/ubuntukylin/pool/partner"

ENV DEBIAN_FRONTEND=noninteractive

# 默认 Ubuntu 官方源（境外 runner 用）；国内构建传 --build-arg APT_MIRROR_BASE=http://mirrors.aliyun.com/ubuntu
RUN sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR_BASE}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR_BASE}|g" \
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
        poppler-utils python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- WPS Office 11.1.0.9662（阿里云 ubuntukylin 社区仓库，单文件 ~301MB）---
# 默认走阿里云（已验证 200）；可用 --build-arg WPS_DEB_BASE 覆盖为其他镜像。
# 若镜像 URL 失效，可手动下载 wps-office_11.1.0.9662_amd64.deb 放到构建目录，
# 并改用: COPY wps-office_11.1.0.9662_amd64.deb /tmp/wps.deb
RUN curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/wps.deb \
        "${WPS_DEB_BASE}/wps-office_11.1.0.9662_amd64.deb" \
    && dpkg -i /tmp/wps.deb || apt-get -f install -y \
    && rm -f /tmp/wps.deb \
    && dpkg -l | grep wps-office

# --- libtiff 兼容（WPS 9662 需要 libtiff.so.5）---
RUN ln -sf /usr/lib/x86_64-linux-gnu/libtiff.so.6 /usr/lib/x86_64-linux-gnu/libtiff.so.5

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
COPY convert_docx2pdf.py entrypoint.sh /opt/wpsrpc-rpc/
COPY tests/e2e_test.sh /opt/wpsrpc-rpc/tests/e2e_test.sh
RUN chmod +x /opt/wpsrpc-rpc/entrypoint.sh /opt/wpsrpc-rpc/tests/e2e_test.sh

# --- 默认工作区 ---
RUN mkdir -p /data
WORKDIR /data

ENTRYPOINT ["/opt/wpsrpc-rpc/entrypoint.sh"]
