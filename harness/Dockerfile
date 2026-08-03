# Fake Sense cloud emulator image (dev/demo/e2e harness). Pure stdlib — no deps.
FROM python:3.14-slim
COPY fake_sense.py /fake_sense.py
# Serves the REST API + realtime WebSocket on one HTTP port. Internal to the stack.
EXPOSE 8080/tcp
CMD ["python3", "/fake_sense.py"]
