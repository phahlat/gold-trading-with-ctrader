FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

WORKDIR /app

STOPSIGNAL SIGTERM

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/logs /app/backtest/results /app/position-data

ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "live", "--no-plot"]
