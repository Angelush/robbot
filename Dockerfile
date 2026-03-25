FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY *.py .
COPY .env .

# Copy data (indexes + ChromaDB)
COPY data/ data/
COPY chroma_db/ chroma_db/

CMD ["python", "bot.py"]
