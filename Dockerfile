FROM alpine:3.20

RUN apk add --no-cache \
        pjproject \
        pjsua \
        py3-pjsua \
        python3 \
        bash \
        ca-certificates \
    && mkdir -p /usr/share/alsa \
    && printf 'pcm.!default { type null }\nctl.!default { type null }\n' \
        > /usr/share/alsa/alsa.conf

# Suppress JACK errors (no audio server in Docker)
ENV JACK_NO_START_SERVER=1
ENV JACK_NO_AUDIO_RESERVATION=1

COPY entrypoint.sh /entrypoint.sh
COPY scripts/ /scripts/
RUN chmod +x /entrypoint.sh

# sipssert mounts scenario dir to /home
WORKDIR /home

ENTRYPOINT ["/entrypoint.sh"]
