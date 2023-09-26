# ChomageBot

## Getting started

1. Clone the repository (`git clone https://github.com/williamlogan77/ChomageBot.git`)

2. Create a `.env` file that contains a discord token (named `token`) and a riot token (named `riot_key`) that sits in the `../Bot` directory, alongside the docker files

3. Build the container with `docker compose build` and run the container with `docker compose up`.

4. If changes are made in cogs, run the `/sync` command in the `chommage_admin` channel to update these to the bot