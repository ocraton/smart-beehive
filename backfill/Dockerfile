FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY profiles.json .
COPY src/ ./src/

CMD ["python", "-m", "src.main"]
