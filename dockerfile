FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py graph.py ner.py ./

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
