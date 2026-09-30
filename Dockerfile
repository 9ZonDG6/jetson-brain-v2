FROM python:3.14-slim-bookworm

WORKDIR /opt/app
COPY vendor/ffmpeg /usr/local/bin/ffmpeg
COPY vendor/ffprobe /usr/local/bin/ffprobe
COPY wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    /wheels/jetson_brain_v2-0.0.1-py3-none-any.whl \
    && rm -rf /wheels

EXPOSE 8080
CMD ["python", "-m", "jetson_brain_v2.app", "--host", "0.0.0.0", "--port", "8080"]
