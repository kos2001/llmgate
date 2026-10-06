FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY llmgate ./llmgate
COPY mockvllm ./mockvllm
COPY mockoidc ./mockoidc
RUN pip install --no-cache-dir .
ENV LLMGATE_DATABASE_PATH=/data/llmgate.db
VOLUME /data
EXPOSE 8080
CMD ["llmgate", "--host", "0.0.0.0", "--port", "8080"]
