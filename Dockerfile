FROM alpine:3.20

RUN apk add --no-cache \
        pjproject \
        pjsua \
        py3-pjsua \
        python3 \
        bash \
        ca-certificates

COPY entrypoint.sh /entrypoint.sh
COPY scripts/ /scripts/
RUN chmod +x /entrypoint.sh

# sipssert mounts scenario dir to /home
WORKDIR /home

ENTRYPOINT ["/entrypoint.sh"]
