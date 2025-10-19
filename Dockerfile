#
# --- STAGE 1: The "Builder" ---
#
FROM python:3.9.18-slim-bullseye AS builder

WORKDIR /opt/builder

# Copy requirements file first
COPY src/requirements.txt .

# Install packages into a target directory
# This uses the pinned versions from your new requirements.txt
RUN pip install --no-cache-dir --target=./packages -r requirements.txt

#
# --- STAGE 2: The "Final" Image ---
#
FROM python:3.9.18-slim-bullseye

WORKDIR /usr/src/app

# Create a non-root user
ARG UID=1001
ARG GID=1001
RUN addgroup --gid $GID kubescaler && \
    adduser --uid $UID --gid $GID --disabled-password --gecos "" kubescaler

# Set env vars to find the packages and their executables
ENV PYTHONPATH=/usr/src/app/packages
ENV PATH=$PATH:/usr/src/app/packages/bin

# Copy packages and source code from the builder
COPY --from=builder /opt/builder/packages ./packages
COPY src/operator.py .

# Switch to the non-root user
USER kubescaler

# Set the entrypoint to run kopf as a module
ENTRYPOINT ["python", "-m", "kopf", "run", "operator.py"]
CMD []