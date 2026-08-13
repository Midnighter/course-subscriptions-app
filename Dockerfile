ARG TAG="3.14.6-slim"
FROM python:${TAG}

WORKDIR /app

COPY requirements.txt ./

RUN set -eux \
    && pip install --no-deps --require-hashes --requirement requirements.txt \
    && rm -rf "${HOME}/.cache"

COPY src/ ./src/

ENV PYTHONPATH="/app/src:${PYTHONPATH}"

CMD ["uvicorn", "--host", "0.0.0.0", "--port", "8000", "--factory", "course_subscriptions.main:create_app"]
