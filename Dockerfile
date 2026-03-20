FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/dgtlmoon/sockpuppetbrowser"

# Pin a specific cloakbrowser pip version to control the bundled Chromium version.
# Each cloakbrowser release ships a specific patched Chromium build.
# See: https://pypi.org/project/cloakbrowser/#history
ARG CLOAKBROWSER_PIP_VERSION

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=DEBUG
# Binary is pre-downloaded here at build time and shared across all users
ENV CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser-cache
# Use patchright backend by default for extra CDP automation signal suppression
ENV CLOAKBROWSER_BACKEND=patchright

USER root

# Install Chromium system library dependencies.
# playwright install-deps is used after pip install to get the correct deps for
# the current Debian version automatically.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xvfb \
    fonts-liberation \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /usr/src/app/requirements.txt

WORKDIR /usr/src/app

# Install Python deps. If CLOAKBROWSER_PIP_VERSION is set, pin to that version.
# cloakbrowser[patchright] adds extra CDP detection suppression on top of canvas/audio patching.
RUN if [ -n "${CLOAKBROWSER_PIP_VERSION}" ]; then \
        pip install --no-cache-dir "cloakbrowser[patchright]==${CLOAKBROWSER_PIP_VERSION}" -r requirements.txt; \
    else \
        pip install --no-cache-dir "cloakbrowser[patchright]" -r requirements.txt; \
    fi

# Install Chromium's system-level library dependencies via Playwright's helper
RUN playwright install-deps chromium

# Pre-download the CloakBrowser patched Chromium binary during the build so the
# container starts instantly without a 200MB download at runtime.
RUN mkdir -p "${CLOAKBROWSER_CACHE_DIR}" \
    && python -m cloakbrowser install \
    && chmod -R 755 "${CLOAKBROWSER_CACHE_DIR}"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY backend/ /usr/src/app/
COPY chrome.json /usr/src/app/

# Run as non-root
RUN useradd -m -d /home/appuser -s /bin/bash appuser \
    && chown -R appuser:appuser /usr/src/app
USER appuser

CMD ["/usr/local/bin/entrypoint.sh"]
