# syntax=docker/dockerfile:1.7
# Skeleton for a `gateway-openai` workload image on the Gittensor compute pool. Spec: MANIFEST.md.
#
# What to replace (search for REPLACE):
#   1. The runtime. Install or build your inference server here (sparkinfer, vLLM, SGLang, ...). The placeholder
#      server below only answers /v1/models so `scripts/validate --build` can smoke the image without a GPU.
#   2. The CMD. entrypoint.sh verifies the manifest's artifacts, then execs whatever CMD says.
#
# What to keep:
#   * No weights baked in. The controller pre-stages every `artifacts[]` entry into the volume the manifest
#     declares (/models here) before the container starts; entrypoint.sh refuses to serve if a sha256 differs.
#   * manifest.yaml copied to /manifest.yaml, so the entrypoint can read `artifacts` and `drain`.
#   * The port matches manifest.yaml `front_door.port` and `health.http.port`.
#
#   docker build -t my-workload:dev .
#   docker run --rm --gpus all -p 8080:8080 -v /path/to/weights:/models:ro my-workload:dev

ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1
# python3-yaml is for entrypoint.sh (it reads /manifest.yaml). REPLACE: add your runtime's packages here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-yaml \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/workload
COPY manifest.yaml /manifest.yaml
COPY entrypoint.sh /opt/workload/entrypoint.sh
# REPLACE: placeholder_server.py stands in for your inference server. Delete it once your runtime is installed.
COPY placeholder_server.py /opt/workload/placeholder_server.py
RUN chmod 0755 /opt/workload/entrypoint.sh

# The controller pre-stages artifacts here; the run spec mounts it read-only.
VOLUME ["/models"]
ENV MODELS_DIR=/models \
    PORT=8080
EXPOSE 8080

LABEL io.gittensor.compute.manifest="/manifest.yaml"

HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/v1/models" || exit 1

ENTRYPOINT ["/opt/workload/entrypoint.sh"]
# REPLACE: the command that starts your server on $PORT, serving /v1/models and /v1/chat/completions.
CMD ["python3", "/opt/workload/placeholder_server.py"]
