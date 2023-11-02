#!/bin/bash
mkdir tmp
python3 utils/create_ranked_graph.py
python3 main.py