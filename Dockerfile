FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyusb==1.3.1
WORKDIR /app
COPY smsgw /app/smsgw
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER root
CMD ["python", "-m", "smsgw"]
