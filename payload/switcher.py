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
COPY_TIMEOUT = 0.6
COPY_POLL_INTERVAL = 0.005
PASTE_SETTLE_TIME = 0.1


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


def set_input_source(target):
    if direct_input_call('SetLayout', target) != target:
        raise RuntimeError('GNOME не выбрал запрошенный язык ввода')


@lru_cache(maxsize=1)
def desktop_tools():
    # Чистые преобразования и --help работают без графической сессии.
    from keyboard_x11 import Keyboard
    import pyperclip
    return Keyboard.connect(), pyperclip


@contextmanager
def saved_text_clipboard(clipboard):
    """Восстановить текстовый буфер также при ошибке копирования/вставки."""
    original = clipboard.paste()
    try:
        yield clipboard
    finally:
        original_error = sys.exc_info()[1]
        try:
            clipboard.copy(original)
        except Exception:
            if original_error is None:
                raise
            # Keep the actual failure (including cancellation) as the cause.
            print('lang-switcher: Не удалось восстановить текстовый буфер', file=sys.stderr)


def get_selected_text(keyboard, clipboard):
    clipboard.copy('')
    keyboard.hotkey('ctrl', 'c')
    # Copy is asynchronous in the target application. Stop as soon as it
    # supplies text instead of paying a fixed delay on every invocation.
    deadline = time.monotonic() + COPY_TIMEOUT
    while True:
        text = clipboard.paste()
        if text:
            return text
        if time.monotonic() >= deadline:
            return ''
        time.sleep(COPY_POLL_INTERVAL)


def paste_text(text, keyboard, clipboard):
    clipboard.copy(text)
    keyboard.hotkey('ctrl', 'v')
    # XSync only acknowledges delivery to the X server, not the application's
    # clipboard request. Keep the text available before restoring the buffer.
    time.sleep(PASTE_SETTLE_TIME)


def process_selection(change_case):
    keyboard, clipboard = desktop_tools()
    keyboard.wait_for_modifiers_release()
    # One transaction avoids restoring and reading the original clipboard
    # between copying the selection and pasting its replacement.
    with saved_text_clipboard(clipboard):
        selected = get_selected_text(keyboard, clipboard)
        if not selected:
            return
        if change_case:
            converted = toggle_case(selected)
        else:
            target = target_layout(selected)
            converted = switch_layout(selected, target)
            # Before pasting: if the extension fails, leave the selection intact.
            set_input_source(target)
        if converted != selected:
            paste_text(converted, keyboard, clipboard)


@contextmanager
def selection_lock():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
    # Stable lock and D-Bus names preserve compatibility with older versions.
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
    except KeyboardInterrupt:
        print('lang-switcher: Операция отменена', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'lang-switcher: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
