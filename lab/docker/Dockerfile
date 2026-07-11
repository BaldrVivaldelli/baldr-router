FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY router /src/router
RUN python -m pip install --no-cache-dir /src/router

ENV BALDR_CLIENT_ID=baldr-lab-container \
    XDG_CONFIG_HOME=/state/config \
    XDG_CACHE_HOME=/state/cache \
    XDG_STATE_HOME=/state/state

CMD ["baldr-router", "lab", "--mode", "full", "--repeat", "3", "--profile", "linux-container"]
