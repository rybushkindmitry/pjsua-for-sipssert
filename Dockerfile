FROM alpine:3.20

RUN apk add --no-cache \
        pjproject \
        pjsua \
        py3-pjsua \
        python3 \
        bash \
        ca-certificates \
    # Suppress ALSA errors (no sound card in Docker)
    && mkdir -p /usr/share/alsa \
    && printf 'pcm.!default { type null }\nctl.!default { type null }\n' \
        > /usr/share/alsa/alsa.conf \
    # Remove JACK runtime to suppress "cannot connect to server" errors
    && apk del --no-cache jack 2>/dev/null; true

COPY entrypoint.sh /entrypoint.sh
COPY scripts/ /scripts/
RUN chmod +x /entrypoint.sh

# sipssert mounts scenario dir to /home
WORKDIR /home

ENTRYPOINT ["/entrypoint.sh"]
