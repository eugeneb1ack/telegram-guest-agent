FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 guest
WORKDIR /app

COPY guest_gateway.py /app/guest_gateway.py
COPY rich_renderer.py /app/rich_renderer.py

RUN mkdir -p /app/runtime /sandbox/inbound \
    && chown -R guest:guest /app /sandbox

USER guest

ENTRYPOINT ["python", "-u", "/app/guest_gateway.py"]
CMD ["--poll"]
