FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY mewcode/ ./mewcode/

RUN pip install --no-cache-dir .

CMD ["python", "-m", "mewcode"]
