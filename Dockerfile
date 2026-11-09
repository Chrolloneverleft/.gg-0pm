FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY . .

# Ensure persistent data directory exists (mount your Railway Volume at /app/data)
RUN mkdir -p /app/data /tmp/matplotlib

CMD ["python", "-u", "bot.py"]
