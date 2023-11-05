FROM python:3.11

USER root

WORKDIR /Bot
COPY ./Bot .

# new comment

RUN apt-get update && apt-get install ffmpeg libsm6 libxext6  -y

RUN python3 -m pip install --no-cache-dir -r  requirements.txt --user

# CMD ["python3", "utils/create_ranked_graph.py", "&&", "python3", "main.py"]

CMD ["sh", "startup.sh"]