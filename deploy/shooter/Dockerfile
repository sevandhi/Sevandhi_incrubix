# Evidence screenshot shooter (compose profile `evidence`). The Playwright image (Apache-2.0) ships the
# browsers but not the Python package, so the matching package version is installed here (Apache-2.0).
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble
RUN pip install --no-cache-dir --break-system-packages playwright==1.62.0
COPY shooter.py /shooter/shooter.py
USER pwuser
EXPOSE 3000
CMD ["python", "/shooter/shooter.py"]
