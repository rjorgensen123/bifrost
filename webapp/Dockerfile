# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.12.14-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# G-318: kjør som non-root — et RCE/skrive-primitiv i appen skal ikke kjøre som uid 0. Appen skriver
# kun til DB + stdout (aldri til filsystemet i request-stien), og ./brand er mount-et read-only, så
# ingen skrive-tilgang trengs. chown /app så pakker/kode er lesbare for den nye brukeren.
RUN useradd -r -u 10001 bifrost && chown -R bifrost:bifrost /app
USER bifrost
EXPOSE 8123
# --timeout 120: SNMP discovery/rescan walks can run well past gunicorn's 30s
# default, especially against slow, large, or partly-unresponsive devices.
CMD ["gunicorn", "-w", "2", "--timeout", "120", "-b", "0.0.0.0:8123", "app:app"]
