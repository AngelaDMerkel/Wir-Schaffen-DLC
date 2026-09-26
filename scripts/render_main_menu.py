#!/usr/bin/env python3
"""Render the README's main-menu SVG from WSDLC's actual terminal frame.

This calls only the screen renderer. It does not discover the game, download
mods, or run an installation. No GUI capture timing or terminal profile is used.
"""
from __future__ import annotations

import argparse
from html import escape
import os
import re
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_installer as installer

COLUMNS, ROWS = 112, 34
CELL_WIDTH, CELL_HEIGHT = 10, 22


def rgb(sequence: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in sequence[2:-1].split(';')[-3:])


def color(value: tuple[int, int, int]) -> str:
    return '#' + ''.join(f'{channel:02x}' for channel in value)


def render() -> str:
    with patch.object(installer.shutil, 'get_terminal_size', return_value=os.terminal_size((COLUMNS, ROWS))):
        frame = installer.full_screen_mode_frame(selected=0, color=True)
    plain = installer.TUI_CONTROL_SEQUENCE_RE.sub('', frame)
    for label in ('SELECT PROGRAM', installer.best_mods.PRESET_NAME,
                  'Package installed mods & maps', "Install AngelaDMerkel's map patch",
                  installer.RESTORE_STOCK_NAME, '[ENTER] launch'):
        if label not in plain:
            raise ValueError(f'main menu is incomplete: missing {label}')

    default_fg, default_bg = rgb(installer.TUI_FOREGROUND), rgb(installer.TUI_BACKGROUND)
    fg, bg, bold = default_fg, default_bg, False
    x = y = cursor = 0
    cells: list[list[tuple[int, str, int, tuple, tuple, bool]]] = [[] for _ in range(ROWS)]

    def text(segment: str) -> None:
        nonlocal x, y
        for character in segment:
            if character == '\n':
                x, y = 0, y + 1
                continue
            width = installer.terminal_display_width(character)
            if not (0 <= y < ROWS) or x + width > COLUMNS:
                raise ValueError('terminal frame exceeds the documented canvas')
            cells[y].append((x, character, width, fg, bg, bold))
            x += width

    for control in installer.TUI_CONTROL_SEQUENCE_RE.finditer(frame):
        text(frame[cursor:control.start()])
        sequence = control.group()
        if sequence.startswith('\x1b[') and sequence.endswith('m'):
            values = [int(value or 0) for value in sequence[2:-1].split(';')]
            index = 0
            while index < len(values):
                code = values[index]
                if code == 0:
                    fg, bg, bold = default_fg, default_bg, False
                elif code == 1:
                    bold = True
                elif code == 22:
                    bold = False
                elif code in (38, 48) and values[index + 1] == 2:
                    value = tuple(values[index + 2:index + 5])
                    if code == 38:
                        fg = value
                    else:
                        bg = value
                    index += 4
                else:
                    raise ValueError(f'unsupported terminal style: {sequence!r}')
                index += 1
        cursor = control.end()
    text(frame[cursor:])

    width, height = COLUMNS * CELL_WIDTH, ROWS * CELL_HEIGHT
    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
        '<title id="title">Wir Schaffen DLC main menu</title>',
        '<desc id="description">Generated from the application screen renderer. All four programs are visible; AngelaDMerkel’s Very Best Mods is selected.</desc>',
        f'<rect width="{width}" height="{height}" fill="{color(default_bg)}"/>',
        '<g font-family="Menlo, DejaVu Sans Mono, monospace" font-size="16" xml:space="preserve">',
    ]
    for row, contents in enumerate(cells):
        start = 0
        while start < len(contents):
            end = start + 1
            while end < len(contents) and contents[end][3:] == contents[start][3:]:
                end += 1
            run = contents[start:end]
            left, foreground, background, weight = run[0][0], *run[0][3:]
            run_width = sum(cell[2] for cell in run) * CELL_WIDTH
            if background != default_bg:
                output.append(f'<rect x="{left * CELL_WIDTH}" y="{row * CELL_HEIGHT}" width="{run_width}" height="{CELL_HEIGHT}" fill="{color(background)}"/>')
            value = ''.join(cell[1] for cell in run)
            # Place each word explicitly: SVG viewers differ in how they
            # preserve leading/repeated spaces, especially in ASCII wordmarks.
            for word in re.finditer(r'\S+', value):
                word_left = left + installer.terminal_display_width(value[:word.start()])
                word_width = installer.terminal_display_width(word.group()) * CELL_WIDTH
                output.append(f'<text x="{word_left * CELL_WIDTH}" y="{row * CELL_HEIGHT + 17}" fill="{color(foreground)}" font-weight="{700 if weight else 400}" textLength="{word_width}" lengthAdjust="spacingAndGlyphs">{escape(word.group())}</text>')
            start = end
    output.extend(['</g>', '</svg>'])
    return '\n'.join(output) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'assets/wir-schaffen-dlc-main.svg')
    parser.add_argument('--check', action='store_true', help='fail if the saved image differs from the current installer')
    args = parser.parse_args()
    rendered = render()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding='utf-8') != rendered:
            parser.exit(1, 'error: README main-menu image is stale; run scripts/render_main_menu.py\n')
        print('README main-menu image matches the current installer and version.')
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
