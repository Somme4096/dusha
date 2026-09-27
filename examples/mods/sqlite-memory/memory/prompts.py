from __future__ import annotations

import copy

_DEFAULT = {
    "evergreen": {
        "open_delimiter": "<evergreen_facts>\n",
        "close_delimiter": "\n</evergreen_facts>\n",
    }
}


def default_prompts() -> dict:
    return copy.deepcopy(_DEFAULT)
