ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}

USER root
ARG DEBIAN_MIRROR=https://deb.debian.org
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates dbus-x11 fonts-noto-cjk libasound2t64 libdbus-1-3 \
    libegl1 libgbm1 libgl1 libgtk-3-0t64 libnss3 libpulse0 libsecret-1-0 \
    libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-xfixes0 libxcb-xinerama0 libxcomposite1 libxcursor1 \
    libxdamage1 libxi6 libxkbcommon-x11-0 libxrandr2 libxrender1 \
    libxss1 libxtst6 novnc openbox python3 supervisor tini \
    websockify x11-utils x11vnc xauth xdg-utils xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY client.deb /tmp/client.deb
RUN dpkg-deb -x /tmp/client.deb / \
    && test -x /opt/wechat/wechat \
    && ln -s /opt/wechat/wechat /usr/bin/wechat \
    && rm /tmp/client.deb \
    && groupadd --gid 10001 wechat-desktop \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/wechat wechat-desktop

COPY desktop.py /opt/wechat-desktop/desktop.py
COPY supervisord.conf /etc/supervisor/wechat-desktop.conf
ENV HOME=/home/wechat \
    DISPLAY=:99 \
    XAUTHORITY=/tmp/wechat-desktop/Xauthority \
    XDG_RUNTIME_DIR=/tmp/runtime-wechat \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai
USER 10001:10001
WORKDIR /home/wechat
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "/opt/wechat-desktop/desktop.py"]
HEALTHCHECK --interval=30s --timeout=8s --start-period=60s --retries=3 \
    CMD ["/usr/bin/python3", "/opt/wechat-desktop/desktop.py", "health"]
