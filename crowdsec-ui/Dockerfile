FROM python:3.12-alpine

WORKDIR /app
COPY app.py /app/app.py

ENV CROWDSEC_UI_BIND=0.0.0.0
ENV CROWDSEC_UI_PORT=8088

EXPOSE 8088
CMD ["python", "/app/app.py"]
