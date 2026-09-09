"""Minimal JSON / filesystem helpers shared by the AS2Biz scripts and notebook."""

import json
import os


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, data):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def makedir(path):
    os.makedirs(path, exist_ok=True)
    return path
