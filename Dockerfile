FROM python:3.11

USER root

WORKDIR /Bot
COPY ./Bot .

# new comment

RUN python3 -m pip install --no-cache-dir -r  requirements.txt --user

RUN python3 utils/create_ranked_graph.py

CMD ["python3", "main.py"]