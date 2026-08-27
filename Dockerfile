# 配电网本体知识推理 Demo —— Docker 镜像
FROM python:3.10-slim

WORKDIR /app

# 依赖层(利用缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 应用代码
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
