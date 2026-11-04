ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG APP_REVISION
ENV APP_REVISION=${APP_REVISION}
WORKDIR /app
COPY bot.py /app/bot.py
COPY deploy/container_entrypoint.py /app/container_entrypoint.py
USER 65532:65532
ENTRYPOINT ["python3", "-B", "-u", "/app/container_entrypoint.py"]
