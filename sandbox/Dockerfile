FROM python:3.12-alpine

RUN apk add --no-cache bash coreutils findutils grep ripgrep \
    && addgroup -g 1000 yyagent \
    && adduser -D -u 1000 -G yyagent yyagent

USER yyagent
WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
