FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir google-colab-cli==0.7.4 \
    && useradd --create-home --uid 10001 colab
ENV HOME=/tmp/colab-home PYTHONUNBUFFERED=1
COPY deploy/colab/entrypoint.sh /usr/local/bin/colab-runner
RUN chmod 755 /usr/local/bin/colab-runner
WORKDIR /workspace
USER colab
ENTRYPOINT ["colab-runner"]
