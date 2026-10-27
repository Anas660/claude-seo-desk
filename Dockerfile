FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY seodesk/ ./seodesk/

RUN python -m pip install --no-cache-dir setuptools

RUN python -m pip install \
    --no-build-isolation \
    --retries 3 \
    --timeout 60 \
    --no-cache-dir \
    --prefix=/install .

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
