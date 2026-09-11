"""
Generate the reference tables in README.md from the code itself.

The settings table and the key map are the two pieces of documentation most
likely to go stale, because both change whenever a feature is added.  Deriving
them from the schema and the key map means they cannot disagree with the
program.  Run this after changing either, and commit the result.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aperture.config import SETTINGS, SETTINGS_BY_KEY
from aperture.ui.dialogs import KEYMAP
from aperture.ui.settings import ACTION, SECTION_ORDER, SECTIONS, SETTING

BEGIN = "<!-- generated:{name} -->"
END = "<!-- /generated:{name} -->"


def settings_table() -> str:
    rows = ["| Section | Setting | Default | Range | Notes |",
            "| --- | --- | --- | --- | --- |"]
    for section in SECTION_ORDER:
        for entry in SECTIONS[section]:
            if entry.kind == SETTING:
                setting = SETTINGS_BY_KEY[entry.key]
                default = setting.format(setting.default)
                if setting.kind in ("int", "float"):
                    span = (f"{setting.minimum:g} to {setting.maximum:g}"
                            f", step {setting.step:g}")
                elif setting.kind == "enum":
                    span = " / ".join(str(c).upper() for c in setting.choices)
                elif setting.kind == "bool":
                    span = "on / off"
                else:
                    span = "text"
                note = " ".join(setting.help.split())
                if setting.restart:
                    note += " Restarts the engine."
                rows.append(f"| {section} | {setting.label} | `{default}` | "
                            f"{span} | {note} |")
            elif entry.kind == ACTION and entry.help:
                note = " ".join(entry.help.split())
                rows.append(f"| {section} | {entry.label} | — | action | {note} |")
    return "\n".join(rows)


def keymap_table() -> str:
    rows = ["| Key | Action |", "| --- | --- |"]
    for key, description in KEYMAP:
        rows.append(f"| `{key}` | {description} |")
    return "\n".join(rows)


def replace_block(text: str, name: str, body: str) -> str:
    begin, end = BEGIN.format(name=name), END.format(name=name)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    replacement = f"{begin}\n{body}\n{end}"
    if not pattern.search(text):
        raise SystemExit(f"README is missing the {name!r} generated block")
    return pattern.sub(lambda _: replacement, text)


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "README.md")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    text = replace_block(text, "settings", settings_table())
    text = replace_block(text, "keymap", keymap_table())
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"updated {path}: {len(SETTINGS)} settings, {len(KEYMAP)} bindings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
