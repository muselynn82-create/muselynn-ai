FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install python-binance requests

CMD ["python", "main.py"]
