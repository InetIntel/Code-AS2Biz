"""Small JSON helpers shared by the RIR collection scripts."""

import json


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
