FROM python:3.10-slim

ARG APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG APT_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

WORKDIR /app

# ── 系统依赖：curl(健康检查) + libpq5(psycopg) ──
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i \
            -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# ── 先只复制依赖文件，利用 Docker 层缓存 ──
COPY requirements.txt .

# ── 安装依赖（全部走清华镜像） ──
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

# ── 最后复制项目代码 ──
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ >/dev/null || exit 1

CMD ["uvicorn", "webui_new.server:app", "--host", "0.0.0.0", "--port", "8000"]
