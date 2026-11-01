FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ecomops ./ecomops
COPY stubs ./stubs
COPY static ./static
COPY contracts ./contracts
RUN useradd --system --uid 10001 app && mkdir -p /app/.data && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)"
CMD ["python", "-m", "uvicorn", "ecomops.main:app", "--host", "0.0.0.0", "--port", "8000"]
