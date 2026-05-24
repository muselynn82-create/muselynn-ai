FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install python-binance requests pandas numpy

CMD ["python", "main.py"]
