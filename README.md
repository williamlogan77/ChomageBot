# ChomageBot

ChomageBot is a small discord bot that I personally host on an AWS EC2 instance that is deployed to a private server for me and my friends. It primarily tracks rankings in the game League of Legends and has implemented slash commands for customisation of users.

The bot is fully encapsulated using docker and should be deployable to any environment.


# Getting started

1. Clone the repository (`git clone https://github.com/williamlogan77/ChomageBot.git`)

2. Create a `.env` file that contains a discord token (named `token`) and a riot token (named `riot_key`) that sits in the `../Bot` directory, alongside the docker files.

3. Build the container with `docker compose build` and run the container with `docker compose up`.

4. If changes are made in cogs, run the `/sync` command in the `chommage_admin` channel to update these to the bot

# To-do

* Migrate from SQLite to PostGresSQL (WIP on a local branch)
* Implement front-end dashboard/control
* Add in persistent view for management of bot
* Find new hosting environment (free EC2 instance almost up)
* Implement local caching using a temp db/redis for calls to riot API

# Notes

If you have come across this repository, please feel free to rework/update features and suggest new implementations.


