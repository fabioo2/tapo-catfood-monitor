FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cat_food_monitor.py .

CMD ["python", "-u", "cat_food_monitor.py"]
