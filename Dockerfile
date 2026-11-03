FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STUDY_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Bricht den Build ab, wenn ein Test fehlschlägt; der alte Container läuft dann weiter.
RUN python -m unittest discover -s tests -t tests -q \
    && rm -rf tests tools \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin study

USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-m", "study.healthcheck"]
CMD ["uvicorn", "study.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
