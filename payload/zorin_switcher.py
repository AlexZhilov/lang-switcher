#!/usr/bin/env python3
"""Преобразование выделенного текста RU/EN и регистра в GNOME 43/X11.

Ctrl+Shift+Пробел: преобразовать текст и выбрать соответствующий язык ввода.
Ctrl+Shift+Q: переключить регистр по первой букве, сохранив язык ввода.
Исходная версия: модификация скрипта Михаила Винакова, 2025.
"""

import argparse
import ast
from contextlib import contextmanager
import fcntl
from functools import lru_cache
from itertools import islice
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time

EN_LAYOUT = (
    "`~@#$%^&"
    "qwertyuiop[]QWERTYUIOP{}asdfghjkl;'\\ASDFGHJKL:\"|zxcvbnm,./ZXCVBNM<>?"
)
RU_LAYOUT = (
    'ёЁ"№;%:?'
    'йцукенгшщзхъЙЦУКЕНГШЩЗХЪфывапролджэ\\ФЫВАПРОЛДЖЭ/ячсмитьбю.ЯЧСМИТЬБЮ,'
)
DBUS_PATH = '/org/gnome/Shell/Extensions/ZorinSwitcher'
DBUS_INTERFACE = 'org.gnome.Shell.Extensions.ZorinSwitcher'


def _translation(primary, secondary):
    # Буквы меняются в обе стороны, при совпадении знаков побеждает
    # выбранное направление. Это сохраняет поведение исходного скрипта.
    table = dict(zip(secondary, primary))
    table.update(zip(primary, secondary))
    return str.maketrans(table)


if len(EN_LAYOUT) != len(RU_LAYOUT):
    raise ValueError('Наборы символов раскладок имеют разную длину')

TRANSLATIONS = {
    'ru': _translation(EN_LAYOUT, RU_LAYOUT),
    'us': _translation(RU_LAYOUT, EN_LAYOUT),
}


def target_layout(text):
    """Выбрать направление по первым трём буквам, пропуская отступы."""
    letters = (char for char in text if char.isalpha() and char in EN_LAYOUT + RU_LAYOUT)
    probe = ''.join(islice(letters, 3)) or text[:3]
    english = sum(char in EN_LAYOUT for char in probe)
    russian = sum(char not in EN_LAYOUT and char in RU_LAYOUT for char in probe)
    return 'ru' if english > russian else 'us'


def switch_layout(text, target=None):
    return text.translate(TRANSLATIONS[target or target_layout(text)])


def toggle_case(text):
    """Как раньше: первая буква задаёт регистр всего выделения."""
    first_letter = next((char for char in text if char.isalpha()), None)
    if first_letter is None:
        return text
    return text.lower() if first_letter.isupper() else text.upper()


def direct_input_call(method, *args):
    result = subprocess.run([
        'gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
        '--object-path', DBUS_PATH, '--method', f'{DBUS_INTERFACE}.{method}',
        *args,
    ], capture_output=True, text=True, timeout=2, check=True)
    response = ast.literal_eval(result.stdout.strip())
    if not isinstance(response, tuple) or len(response) != 1 or not isinstance(response[0], str):
        raise RuntimeError('Некорректный ответ модуля GNOME')
    return response[0]


def current_input_source():
    return direct_input_call('GetLayout')


def set_input_source(target):
    if direct_input_call('SetLayout', target) != target:
        raise RuntimeError('GNOME не выбрал запрошенный язык ввода')


def match_input_source(text):
    set_input_source(target_layout(text))


@lru_cache(maxsize=1)
def desktop_tools():
    # Чистые преобразования и --help работают без графической сессии.
    from keyboard_x11 import Keyboard
    import pyperclip
    return Keyboard.connect(), pyperclip


@contextmanager
def saved_text_clipboard():
    """Восстановить текстовый буфер также при ошибке копирования/вставки."""
    _, clipboard = desktop_tools()
    original = clipboard.paste()
    try:
        yield clipboard
    finally:
        clipboard.copy(original)


def get_selected_text():
    keyboard, _ = desktop_tools()
    with saved_text_clipboard() as clipboard:
        clipboard.copy('')
        time.sleep(0.1)
        keyboard.hotkey('ctrl', 'c')
        time.sleep(0.2)
        return clipboard.paste()


def paste_text(text):
    if not text:
        return
    keyboard, _ = desktop_tools()
    with saved_text_clipboard() as clipboard:
        clipboard.copy(text)
        time.sleep(0.1)
        keyboard.hotkey('ctrl', 'v')
        time.sleep(0.1)


def release_modifiers():
    keyboard, _ = desktop_tools()
    for key in ('ctrl', 'ctrlright', 'ctrlleft', 'shift'):
        try:
            keyboard.keyUp(key, _pause=False)
        except Exception:
            # Уборка не должна скрывать исходную ошибку приложения.
            continue
    time.sleep(0.05)


def process_selection(change_case):
    desktop_tools()
    try:
        time.sleep(0.1)
        selected = get_selected_text()
        if not selected:
            return
        if change_case:
            converted = toggle_case(selected)
        else:
            target = target_layout(selected)
            converted = switch_layout(selected, target)
            # До вставки: язык уже соответствует преобразованному тексту.
            # Если модуль недоступен, исходный текст остаётся выделенным.
            set_input_source(target)
        paste_text(converted)
    finally:
        release_modifiers()


@contextmanager
def selection_lock():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
    path = runtime / f'zorin-switcher-{os.getuid()}.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('Недопустимый файл блокировки')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--layout', '-l', action='store_true', help='Изменить раскладку текста и язык ввода (по умолчанию)')
    mode.add_argument('--case', '-c', action='store_true', help='Изменить регистр выделенного текста')
    args = parser.parse_args(argv)
    try:
        with selection_lock() as acquired:
            if not acquired:
                return 0
            process_selection(args.case)
    except Exception as error:
        print(f'Zorin Switcher: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
