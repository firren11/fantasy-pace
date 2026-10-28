#!/bin/zsh
cd "$(dirname "$0")"
.venv/bin/python -W ignore pace.py >> pace.log 2>&1
