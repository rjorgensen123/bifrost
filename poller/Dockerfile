# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.12.14-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# G-318: kjør som non-root — polleren skriver kun til DB + stdout. chown /app for lesbarhet.
RUN useradd -r -u 10001 bifrost && chown -R bifrost:bifrost /app
USER bifrost
CMD ["python", "-u", "poller.py"]
