FROM debian:bookworm-slim AS builder

ARG PJSIP_VERSION=2.14.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        ca-certificates \
        python3-dev \
        swig \
        libssl-dev \
        libsrtp2-dev \
        libopus-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${PJSIP_VERSION} \
        https://github.com/pjsip/pjproject.git /pjproject

WORKDIR /pjproject

# Disable audio/video device dependencies not needed in Docker
RUN cat > pjlib/include/pj/config_site.h <<'EOF'
#define PJMEDIA_HAS_VIDEO 0
#define PJMEDIA_AUDIO_DEV_HAS_ALSA 0
#define PJMEDIA_AUDIO_DEV_HAS_PORTAUDIO 0
EOF

RUN ./configure \
        --prefix=/usr/local \
        --enable-shared \
        --with-ssl=/usr \
        --with-srtp=/usr \
        --disable-video \
        --disable-v4l2 \
        --disable-sound \
    && make dep \
    && make -j"$(nproc)" \
    && make install

# Build Python bindings
RUN cd pjsip-apps/src/swig/python && make && pip3 install --break-system-packages .

# ---------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        libsrtp2-1 \
        libopus0 \
        python3 \
        python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /usr/local/lib/python3*/dist-packages/ /usr/local/lib/python3.11/dist-packages/

RUN ldconfig

COPY entrypoint.sh /entrypoint.sh
COPY scripts/ /scripts/
RUN chmod +x /entrypoint.sh

# sipssert mounts scenario dir to /home
WORKDIR /home

ENTRYPOINT ["/entrypoint.sh"]
