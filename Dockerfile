FROM python:latest

USER root

WORKDIR /Bot
COPY ./Bot .


RUN python3 -m pip install --no-cache-dir -r  requirements.txt --user


CMD [ "python3", "main.py" ]
