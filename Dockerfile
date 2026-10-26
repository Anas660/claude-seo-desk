FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY seodesk/ ./seodesk/

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

ENV PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEODESK_HOME=/data

RUN mkdir -p /data

COPY --from=builder /install /install

WORKDIR /data

VOLUME ["/data"]

EXPOSE 5380

ENTRYPOINT ["seodesk"]
