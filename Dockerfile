FROM python:3.11-slim

WORKDIR /app

# 依赖先单独装，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY aether ./aether
COPY run.py pyproject.toml ./

# 容器内 /data 持久化模型、运行时、账本与身份
ENV AETHER_HOME=/data
# 容器必然对外提供服务，强制开启共享（会自动生成 api_key，见 docker logs）
ENV AETHER_SHARE=1
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8737

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8737/healthz', timeout=3).status==200 else 1)"

# 不用 --host 覆盖：共享开关已在配置层生效，绑 0.0.0.0 并强制鉴权
CMD ["python", "run.py", "--no-window", "--port", "8737"]
