FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY news_briefing.py config.yaml ./

# output blijft staan in /app/output, wordt via volume gemount naar de host
CMD ["python3", "news_briefing.py"]
