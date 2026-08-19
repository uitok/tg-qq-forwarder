FROM node:22-alpine AS web-build

WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py server.py README.md .env.example ./
COPY --from=web-build /web/dist ./web/dist

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
