"""Compatibility with malformed filtered JSON emitted by lshw 02.18.x.

Handle the structural defects recognized by pg_diag.executors.shell while
protecting string contents. This parser does not depend on pg_diag's private API.
Call it only for lshw sources after strict JSON parsing has failed.
"""

import json
import re
from typing import Any

_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def parse_legacy_lshw_json(text: str) -> Any:
    if text.strip() == ']':
        return []

    # Work on opaque string tokens so braces, commas and scalar-looking text
    # inside hardware names/values cannot be mistaken for broken JSON structure.
    strings = []

    def protect(match):
        strings.append(match[0])
        return f'"{len(strings) - 1}"'

    repaired = _JSON_STRING.sub(protect, text)
    repaired = re.sub(r'}\s+{', '}}, {', repaired)
    repaired = re.sub(
        r'("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?|true|false|null)\s+{',
        r'\1}, {',
        repaired,
    )
    # A filtered parent with several matching children can leave its original
    # terminator between the flattened last child and the next record.
    repaired = re.sub(r'}\s*,\s*}\s*,\s*{', '}, {', repaired)
    repaired = re.sub(r',\s*}\s*]\s*$', '\n]', repaired)
    repaired = re.sub(r',\s*]\s*$', '\n]', repaired)
    repaired = _close_unterminated_object(repaired)
    repaired = _JSON_STRING.sub(lambda match: strings[int(match[0][1:-1])], repaired)
    return json.loads(repaired)


def _close_unterminated_object(text: str) -> str:
    """Close one object omitted immediately before the top-level array end."""
    if not text.lstrip().startswith('[') or not text.rstrip().endswith(']'):
        return text

    object_depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == '{':
            object_depth += 1
        elif character == '}':
            object_depth -= 1

    if object_depth != 1:
        return text
    array_end = text.rfind(']')
    return text[:array_end] + '}\n' + text[array_end:]
