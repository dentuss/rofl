FROM python:3.11-slim

# System deps for pandas/numpy/sklearn wheels (only if needed for ARM)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -fs /usr/share/zoneinfo/UTC /etc/localtime

WORKDIR /app

# Install Python deps first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code
COPY core/ ./core/
COPY research/ ./research/
COPY bot.py test_parity.py tg_control.py sleeves_paper.py xs_paper.py collector.py ./

# Cache and state directories — mounted as volumes in compose
RUN mkdir -p /app/.cache /app/state /app/logs

# Bot reads STATE_FILE and LOG_FILE from env; point them at the volumes
ENV STATE_FILE=/app/state/bot_state.json \
    LOG_FILE=/app/logs/bot.log \
    PYTHONUNBUFFERED=1

# Default command; the composes set MODE/preset/symbol per service.
CMD ["python3", "bot.py"]
