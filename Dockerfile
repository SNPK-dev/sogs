# Use an official Python runtime as a parent image
FROM python:3.9-slim-bullseye AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /usr/src/app

# Install system dependencies
# Add any system libraries needed by sogs-compress or other dependencies
# For example: RUN apt-get update && apt-get install -y libsome-dependency
# Placeholder for sogs-compress installation:
# If sogs-compress is available via apt:
# RUN apt-get update && apt-get install -y <sogs-compress-package-name> && rm -rf /var/lib/apt/lists/*
# If sogs-compress is a binary you provide (e.g., in a 'bin' directory):
# COPY bin/sogs-compress /usr/local/bin/sogs-compress
# RUN chmod +x /usr/local/bin/sogs-compress
# If sogs-compress needs to be built from source, add those steps here.
# IMPORTANT: The actual sogs-compress installation method is needed here.
# For now, this Dockerfile assumes sogs-compress will be made available in the PATH.

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Ensure the 'uploads' and 'output' directories exist and have appropriate permissions if needed
# These are created by app.py on startup, but good to be explicit if necessary.
RUN mkdir -p /usr/src/app/uploads /usr/src/app/output
# RUN chown -R <someuser>:<somegroup> /usr/src/app/uploads /usr/src/app/output
# Consider running as a non-root user for security
# RUN addgroup --system app && adduser --system --group app
# USER app

# Expose the port the app runs on
# Gunicorn will bind to 0.0.0.0:5000 by default through app.py's host/port settings if __main__ is run
# Or we can specify it in the CMD. Let's use Gunicorn directly.
EXPOSE 5000

# Run app.py when the container launches using Gunicorn
# The number of workers can be adjusted based on the server's resources
# Gunicorn needs to know where the Flask app instance is: `app:app` means "in file app.py, the Flask instance is named app"
# The `threaded=True` in app.py's app.run() is for Flask's dev server. Gunicorn manages its own workers/threads.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--worker-class", "gthread", "app:app"]
