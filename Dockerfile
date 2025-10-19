#
# --- STAGE 1: The "Builder" ---
#
# Slim-bullseye (Debian 11) image to build dependencies.
FROM python:3.9-slim-bullseye AS builder

WORKDIR /app

# Install dependencies into a separate prefix
# This makes it easy to copy the complete environment
COPY src/requirements.txt .
RUN pip install --no-cache-dir --prefix="/install" -r requirements.txt

# Copy the operator source code
COPY src/operator.py operator.py

#
# --- STAGE 2: The "Final" Image ---
#
# This is Google's "distroless" base image. It contains Python 3.9 (from debian11)
# and nothing else (no shell, no apt, no-curl, etc.).
FROM gcr.io/distroless/python3-debian11

WORKDIR /app

# Copy the installed packages from the builder stage
COPY --from=builder /install ./

# Copy the operator source code from the builder stage
COPY --from=builder /app/operator.py .

# Set the PYTHONPATH to find the installed packages
ENV PYTHONPATH=/app/lib/python3.9/site-packages

# Run as a non-root user (UID 1001). This is a standard non-root UID.
USER 1001

# Set the entrypoint to the 'kopf' executable, which was installed
# in the /app/bin directory by pip.
ENTRYPOINT ["/usr/bin/python3", "-m", "kopf", "run", "operator.py"]
CMD []