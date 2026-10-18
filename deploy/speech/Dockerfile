FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 espeak-ng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY deploy/speech/requirements.txt /srv/speech-requirements.txt
RUN pip install --no-cache-dir -r /srv/speech-requirements.txt
COPY src/speech_service /srv/speech_service
RUN useradd --create-home speech && mkdir /models && chown speech:speech /models
USER speech
ENV SPEECH_MODEL_DIR=/models
EXPOSE 8090
CMD ["uvicorn", "speech_service.server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--ws-max-size", "196608", "--ws-max-queue", "4"]
