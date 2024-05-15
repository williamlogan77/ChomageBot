#!/bin/bash
mkdir -p tmp
python3 utils/create_ranked_graph.py
python3 -u main.py