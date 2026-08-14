# AIOps relay node, as a container.
#
# The agent is stdlib-only, so this is a base image, one file, and nothing
# else — no package installs, no build stage, nothing to keep patched but the
# base itself.
FROM python:3.12-alpine

# Runs unprivileged. The relay opens outbound TCP connections; it has never
# needed a capability, and if it ever appears to, that is a bug to look at
# rather than a line to add here.
RUN adduser -S -H -D aiops-relay

COPY aiops_relay_node.py /opt/aiops-relay/aiops_relay_node.py

# The credential lands here. Mount a volume on it, or the node re-enrols on
# every recreate and burns an enrolment token each time.
ENV AIOPS_RELAY_STATE_DIR=/var/lib/aiops-relay
RUN mkdir -p /var/lib/aiops-relay && chown aiops-relay /var/lib/aiops-relay
VOLUME ["/var/lib/aiops-relay"]

USER aiops-relay
ENTRYPOINT ["python3", "/opt/aiops-relay/aiops_relay_node.py"]
