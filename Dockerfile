# TG 机器人管理面板 —— Docker 镜像
#
# 用法（推荐走 docker-compose，见 docker-compose.yml）：
#     docker compose up -d
#
# 或者不用 compose：
#     docker build -t tgpanel .
#     docker run -d --name tgpanel --restart unless-stopped \
#       -p 8080:8080 \
#       -v "$PWD/config.json:/app/config.json" \
#       -v "$PWD/bots.json:/app/bots.json" \
#       -v "$PWD/merchants.json:/app/merchants.json" \
#       -v "$PWD/data:/app/data" \
#       tgpanel
#
# ★ 先用 deploy.sh 在宿主机上跑一次生成好 config.json（里面有密码），
#   再 docker compose up —— 不然容器里会现生成一个，密码得去日志里翻。
# ★ 数据（bots.json / merchants.json / data/）都在挂载卷里，删容器不丢。

FROM python:3.11-slim

WORKDIR /app

# 日志不加缓冲，docker logs 能实时看到
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ★ 依赖单独一层：改代码不会让这层缓存失效，重装很快
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 面板端口。★ config.json 里的 host 必须是 0.0.0.0，否则容器外面连不上
EXPOSE 8080

CMD ["python", "主程序.py"]
