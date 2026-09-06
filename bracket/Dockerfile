FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests

COPY bracket.py report_template.html ./

ENTRYPOINT ["python3", "bracket.py"]
