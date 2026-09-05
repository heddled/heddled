# Jarvis's shell. Deliberately unlike the Heddled image: no Heddled source, no
# database, no credentials, and one mounted directory it shares with the file
# browser. See sandbox/server.py for why the shell does not run in the main
# container.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_WORK=/work \
    SANDBOX_PORT=8080

# What a build loop actually reaches for. Kept short on purpose — every tool
# here is one Jarvis can run, and the image is smaller for the ones left out.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq unzip \
    && rm -rf /var/lib/apt/lists/*

# Not root — but the drop happens in the server, not here. `/work` is bind
# mounted from the host and arrives owned by root, so a container that started
# unprivileged could not write to its own workspace. server.py takes the
# directory and then drops, before it serves anything.
RUN useradd --create-home --uid 10001 jarvis \
    && mkdir -p /work && chown jarvis:jarvis /work

COPY server.py /srv/server.py
WORKDIR /work
EXPOSE 8080
CMD ["python", "/srv/server.py"]
