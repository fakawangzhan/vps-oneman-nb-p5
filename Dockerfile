FROM python:3.12-slim AS build
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN python -m venv /opt/venv
COPY --from=build /wheels /wheels
COPY requirements.txt /app/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /app/requirements.txt && rm -rf /wheels
WORKDIR /app
COPY vps_one ./vps_one
RUN mkdir -p /app/data && chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=5 CMD ["python","-c","import urllib.request;urllib.request.urlopen('http://127.0.0.1:9080/healthz',timeout=2)"]
CMD ["uvicorn","vps_one.main:app","--host","0.0.0.0","--port","9080","--workers","1","--proxy-headers"]
