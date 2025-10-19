FROM python:3.9.18-slim-bookworm

# Set a working directory
WORKDIR /usr/src/app

# Define arguments for user and group IDs for better flexibility
ARG UID=1001
ARG GID=1001

# Create a non-root user and group for the application
# --disabled-password ensures the user cannot be logged into
# --gecos "" prevents it from asking for user information
RUN addgroup --gid $GID kubescaler && \
    adduser --uid $UID --gid $GID --disabled-password --gecos "" kubescaler

# Copy source code and set ownership in a single layer for efficiency
# This ensures the non-root user can read the file.
COPY --chown=kubescaler:kubescaler src/operator.py .

# Install dependencies
RUN pip install --no-cache-dir kopf kubernetes pytz

# Switch to the non-root user
# Any subsequent commands (like CMD) will run as this user
USER kubescaler

# Set the command to run the operator.
# The --all-namespaces flag is removed and will be managed in the K8s manifest.
CMD ["kopf", "run", "operator.py"]