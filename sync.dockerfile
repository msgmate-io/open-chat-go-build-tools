# syntax=docker/dockerfile:1
# One-shot init service that materializes integration checkouts and generates
# the Go workspace for the selected profile before backend/frontend start.
#
# The build context is the open-chat-go repository root and this tool is
# vendored as the `development/build-tools` submodule.
FROM python:3.12-alpine

RUN apk add --no-cache git openssh-client
COPY development/build-tools /build-tools
RUN pip install --no-cache-dir /build-tools

WORKDIR /workspace
ENTRYPOINT ["openchat-integrations"]
CMD ["prepare"]
