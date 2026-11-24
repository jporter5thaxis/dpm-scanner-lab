FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends v4l-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-jetson-lock.txt .
RUN pip install --no-cache-dir -r requirements-jetson-lock.txt
COPY backend/__init__.py backend/camera.py backend/config.py backend/vision.py ./backend/
COPY camera_lab ./camera_lab
ENV CAMERA_LAB_DATA=/captures PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home lab && install -d -o lab -g lab /captures
USER lab
EXPOSE 8097
CMD ["python", "-m", "uvicorn", "camera_lab.app:app", "--host", "0.0.0.0", "--port", "8097"]
