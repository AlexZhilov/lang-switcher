#!/usr/bin/env python3
"""Per-user installation of lang-switcher; sudo is used only for APT."""
import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = 'AlexZhilov/lang-switcher'
EXTENSION_UUID = 'lang-switcher@local'
MEDIA = 'org.gnome.settings-daemon.plugins.media-keys'
CUSTOM_SCHEMA = MEDIA + '.custom-keybinding:'
CUSTOM_ROOT = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
INPUT = 'org.gnome.desktop.input-sources'
SHELL = 'org.gnome.shell'
APT_PACKAGES = ('python3-venv', 'xclip', 'libglib2.0-bin', 'bash-completion')


class InstallError(RuntimeError):
    pass


class Console:
    def __init__(self, stream=None):
        self.stream = stream
        self.verbose = False
        self.log_file = None
        self.log_path = None

    def paint(self, text, code):
        stream = self.stream or sys.stdout
        if stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
            return f'\033[{code}m{text}\033[0m'
        return text

    def line(self, text=''):
        print(text, file=self.stream or sys.stdout, flush=True)

    def title(self, version=None):
        self.line(self.paint('lang-switcher', '1;36') + (f' {version}' if version else ''))
        self.line()

    def step(self, number, text):
        self.line(f'  {self.paint(str(number) + "/3", "36")}  {text}')

    def ok(self, text):
        self.line(f'{self.paint("✓", "32")} {text}')

    def warn(self, text):
        self.line(f'{self.paint("!", "33")} {text}')

    def note(self, text):
        self.line(f'  {text}')

    def error(self, text):
        print(f'{self.paint("✗", "31")} {text}', file=self.stream or sys.stderr, flush=True)


UI = Console()


def layout_pairs(home):
    return ((home / '.local/share/zorin-switcher', home / '.local/share/lang-switcher'),
            (home / '.local/bin/zorin-switcher', home / '.local/lib/lang-switcher'))


def data_root(home):
    old, new = layout_pairs(home)[0]
    return new if new.exists() or not old.exists() else old


def layout_plan(home):
    """Validate every root before creating directories or moving anything."""
    moves = []
    for old, new in layout_pairs(home):
        for path in (old, new):
            if not path.parent.resolve().is_relative_to(home.resolve()):
                raise InstallError(f'Каталог программы выходит за пределы HOME: {path}')
            if path.is_symlink():
                if path != old or path.resolve() != new or not new.is_dir():
                    raise InstallError(f'Недопустимая ссылка каталога программы: {path}')
            elif path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()):
                raise InstallError(f'Недопустимый каталог программы: {path}')
        if old.is_dir() and not old.is_symlink():
            if new.exists():
                raise InstallError(f'Найдены два отдельных каталога: {old} и {new}. Перенос остановлен.')
            moves.append((old, new))
    return moves


@contextmanager
def migration_lock():
    # Stable name shared with older switchers: do not move code mid-selection.
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
    path = runtime / f'zorin-switcher-{os.getuid()}.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                         os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError('Недопустимый файл блокировки переключателя')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError('Переключатель обрабатывает текст. Повторите установку.') from error
        yield
    finally:
        os.close(descriptor)


def migrate_layout(home):
    moves = layout_plan(home)
    if not moves:
        return
    completed = []
    with migration_lock():
        try:
            for old, new in moves:
                new.parent.mkdir(parents=True, exist_ok=True)
                alias = old.with_name(old.name + '.migrate-' + uuid.uuid4().hex)
                try:
                    alias.symlink_to(os.path.relpath(new, old.parent), target_is_directory=True)
                    old.rename(new)
                    completed.append((old, new))
                    alias.replace(old)
                finally:
                    alias.unlink(missing_ok=True)
        except BaseException:
            for old, new in reversed(completed):
                if old.is_symlink() and old.resolve() == new:
                    old.unlink()
                new.rename(old)
            raise
    old, new = layout_pairs(home)[0]
    if UI.log_path is not None and UI.log_path.is_relative_to(old):
        UI.log_path = new / UI.log_path.relative_to(old)
    UI.note('Прежняя установка перенесена в каталоги lang-switcher')


@contextmanager
def operation_log(home):
    layout_plan(home)
    data = data_root(home)
    folder = data / 'logs'
    if folder.is_symlink() or not folder.resolve().is_relative_to(data.resolve()):
        raise InstallError('Недопустимый каталог журналов')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    path = folder / f'install-{stamp}.log'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    UI.log_path = path
    with os.fdopen(descriptor, 'w', encoding='utf-8') as log:
        UI.log_file = log
        try:
            yield
        finally:
            UI.log_file = None


def run_logged(args, description, check=True):
    command = [str(arg) for arg in args]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace')
    detail = result.stdout or ''
    if UI.log_file is not None:
        UI.log_file.write('$ ' + shlex.join(command) + '\n' + detail + '\n')
        UI.log_file.flush()
    if UI.verbose and detail:
        UI.line(detail.rstrip())
    if check and result.returncode:
        # Without a persistent log (e.g. developer smoke check), retain the
        # diagnostic output in the exception instead of silently discarding it.
        suffix = f'\n{detail[-4000:].strip()}' if UI.log_file is None else ''
        raise InstallError(f'{description}: код {result.returncode}.{suffix}')
    return result


def installation_summary(restart, backup):
    UI.line()
    if restart:
        UI.warn('Установлено · требуется перезапуск GNOME')
    else:
        UI.ok('Готово · можно пользоваться')
    UI.note('Ctrl+Shift+Пробел  раскладка и язык')
    UI.note('Ctrl+Shift+Q       регистр')
    if restart:
        UI.line()
        UI.note('Ctrl+Shift+Пробел заработает после этого шага:')
        UI.note('Английская раскладка → Alt+F2 → r → Enter')
        UI.note('Затем: lang-switcher status')
    UI.note('Откат: lang-switcher rollback')
    if UI.verbose:
        UI.note(f'Копия: {backup}')
        if UI.log_path is not None:
            UI.note(f'Лог: {UI.log_path}')


def execute(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def output(args):
    return execute(args, capture_output=True, text=True).stdout.strip()


def variant(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return repr(value)


def parse_variant(raw):
    raw = re.sub(r'^@[a-z{}()]+\s+', '', raw)
    if raw in ('true', 'false'):
        return raw == 'true'
    return ast.literal_eval(raw)


class Settings:
    def get_raw(self, schema, key):
        return output(['gsettings', 'get', schema, key])

    def get(self, schema, key):
        return parse_variant(self.get_raw(schema, key))

    def set_raw(self, schema, key, raw):
        command = ['gsettings', 'set', schema, key, raw]
        if UI.log_file is not None:
            run_logged(command, 'Сохранение настроек')
        else:
            execute(command)


def command_mode(command):
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    names = {Path(word).name for word in words}
    if 'switch_case.sh' in names:
        return 'case'
    if 'switch_layout.sh' in names:
        return 'layout'
    if words and Path(words[0]).name == 'lang-switcher' and words[1:] in (['layout'], ['case']):
        return words[1]
    if ('zorin_switcher.py' in names or
            any(Path(word).parts[-3:] == ('lib', 'lang-switcher', 'switcher.py') for word in words)):
        return 'case' if '--case' in words or '-c' in words else 'layout'
    return None


def accelerator(binding):
    aliases = {'primary': 'control', 'ctrl': 'control'}
    modifiers = frozenset(aliases.get(tag.lower(), tag.lower())
                          for tag in re.findall(r'<([^>]+)>', binding))
    key = re.sub(r'<[^>]+>', '', binding).lower()
    return modifiers, key


def legacy_extension_ids(home):
    """Discover older copies by their API, without embedding a user's UUID."""
    root = home / '.local/share/gnome-shell/extensions'
    found = set()
    if root.is_dir():
        for directory in root.iterdir():
            if directory.name == EXTENSION_UUID or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                metadata = json.loads((directory / 'metadata.json').read_text())
                source = (directory / 'extension.js').read_text()
            except (OSError, ValueError, UnicodeError):
                continue
            if (metadata.get('uuid') == directory.name and
                    'org.gnome.Shell.Extensions.ZorinSwitcher' in source and
                    '/org/gnome/Shell/Extensions/ZorinSwitcher' in source):
                found.add(directory.name)
    return found


def managed_files(home):
    extension = home / '.local/share/gnome-shell/extensions' / EXTENSION_UUID
    files = {
        extension / 'extension.js', extension / 'metadata.json',
        home / '.local/bin/switch_layout.sh', home / '.local/bin/switch_case.sh',
        home / '.local/bin/lang-switcher',
        home / '.local/share/bash-completion/completions/lang-switcher',
    }
    # Historical snapshots keep their original absolute paths. Upgrade aliases
    # make those paths usable by both the old manager and the new one.
    for data in layout_pairs(home)[0]:
        files.update(data / name for name in ('manager.py', 'bootstrap.sh', 'current', 'install.json'))
    for app in layout_pairs(home)[1]:
        files.update(app / name for name in ('zorin_switcher.py', 'switcher.py', 'keyboard_x11.py', 'INSTALL-README.txt'))
        files.update(app / 'direct-gnome' / EXTENSION_UUID / name for name in ('extension.js', 'metadata.json'))
    return files


def validate_destination(target, home):
    if target not in managed_files(home) or not target.parent.resolve().is_relative_to(home.resolve()):
        raise InstallError(f'Отказ изменять посторонний путь: {target}')


def validate_setting(schema, key):
    allowed = {MEDIA: {'custom-keybindings'}, INPUT: {'sources', 'mru-sources'},
               SHELL: {'enabled-extensions', 'disabled-extensions'}}
    if schema in allowed and key in allowed[schema]:
        return
    if (schema.startswith(CUSTOM_SCHEMA + CUSTOM_ROOT) and
            re.fullmatch(r'[A-Za-z0-9_-]+/', schema[len(CUSTOM_SCHEMA + CUSTOM_ROOT):]) and
            key in ('name', 'command', 'binding')):
        return
    raise InstallError('Отказ изменять посторонний параметр GSettings')


def settings_plan(settings, home):
    """Reuse known old bindings; preserve unrelated shortcuts and sources."""
    if settings.get(SHELL, 'disable-user-extensions'):
        raise InstallError('Пользовательские расширения GNOME отключены. Включите их в приложении «Расширения» и повторите установку.')
    existing = list(dict.fromkeys(settings.get(MEDIA, 'custom-keybindings')))
    records = {}
    for path in existing:
        schema = CUSTOM_SCHEMA + path
        records[path] = {key: settings.get(schema, key) for key in ('name', 'command', 'binding')}
    desired = {'layout': '<Control><Shift>space', 'case': '<Control><Shift>q'}
    desired_accels = {accelerator(chord) for chord in desired.values()}
    recognized = {path: command_mode(record['command']) for path, record in records.items()}
    for path, record in records.items():
        if recognized[path] is None and accelerator(record['binding']) in desired_accels:
            raise InstallError(f'Горячая клавиша {record["binding"]} уже занята: {record["name"]}. Освободите её в настройках GNOME и повторите установку.')
    active = list(existing)
    plan = []
    for mode, binding in desired.items():
        matches = [path for path in existing if recognized[path] == mode]
        if matches:
            path = matches[0]
            active = [item for item in active if item not in matches[1:]]
        else:
            path = CUSTOM_ROOT + f'lang-switcher-{mode}/'
            index = 1
            while path in records or path in active:
                path = CUSTOM_ROOT + f'lang-switcher-{mode}-{index}/'
                index += 1
            active.append(path)
        schema = CUSTOM_SCHEMA + path
        plan.extend((schema, key, value) for key, value in (
            ('name', 'Switch Layout' if mode == 'layout' else 'Switch Case'),
            ('command', shlex.quote(str(home / '.local/bin/lang-switcher')) + ' ' + mode),
            ('binding', binding),
        ))
    plan.append((MEDIA, 'custom-keybindings', active))
    sources = list(settings.get(INPUT, 'sources'))
    for language in ('us', 'ru'):
        if ('xkb', language) not in sources:
            sources.append(('xkb', language))
    plan.append((INPUT, 'sources', sources))
    legacy = legacy_extension_ids(home)
    enabled = [item for item in settings.get(SHELL, 'enabled-extensions') if item not in legacy]
    if EXTENSION_UUID not in enabled:
        enabled.append(EXTENSION_UUID)
    disabled = [item for item in settings.get(SHELL, 'disabled-extensions') if item != EXTENSION_UUID]
    disabled.extend(item for item in sorted(legacy) if item not in disabled)
    plan.extend(((SHELL, 'disabled-extensions', disabled), (SHELL, 'enabled-extensions', enabled)))
    return plan


def atomic_copy(source, target, mode=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.install-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temp)
        if mode is not None:
            temp.chmod(mode)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


class Transaction:
    def __init__(self, home, settings, directory):
        self.home, self.settings, self.directory = home, settings, directory
        if not directory.parent.resolve().is_relative_to(home.resolve()):
            raise InstallError('Каталог резервных копий выходит за пределы HOME')
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.parent.chmod(0o700)
        directory.mkdir(mode=0o700)
        (directory / 'files').mkdir(mode=0o700)
        self.state = {'version': 1, 'home': str(home), 'files': {}, 'settings': []}
        self.save()

    def save(self):
        temp = self.directory / 'state.json.tmp'
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        temp.chmod(0o600)
        temp.replace(self.directory / 'state.json')

    def remember_file(self, target):
        validate_destination(target, self.home)
        key = str(target)
        if key in self.state['files']:
            return
        if target.is_symlink():
            entry = {'kind': 'symlink', 'target': os.readlink(target)}
        elif target.is_file():
            name = f'files/{len(self.state["files"])}'
            shutil.copy2(target, self.directory / name)
            entry = {'kind': 'file', 'backup': name}
        elif target.exists():
            raise InstallError(f'Ожидался файл, но найден каталог: {target}')
        else:
            entry = {'kind': 'absent'}
        self.state['files'][key] = entry
        self.save()

    def copy(self, source, target, mode):
        self.remember_file(target)
        atomic_copy(source, target, mode)

    def link_runtime(self, runtime, target):
        self.remember_file(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + '.install-' + uuid.uuid4().hex)
        try:
            temp.symlink_to(os.path.relpath(runtime, target.parent))
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def remember_setting(self, schema, key):
        validate_setting(schema, key)
        if not any(item['schema'] == schema and item['key'] == key for item in self.state['settings']):
            self.state['settings'].append({'schema': schema, 'key': key, 'raw': self.settings.get_raw(schema, key)})
            self.save()

    def set(self, schema, key, value):
        raw = variant(value)
        if self.settings.get(schema, key) == value:
            return
        self.remember_setting(schema, key)
        record = next(item for item in self.state['settings'] if item['schema'] == schema and item['key'] == key)
        record['after'] = raw
        self.save()
        self.settings.set_raw(schema, key, raw)


def read_snapshot(directory, home):
    directory = directory.resolve()
    state = json.loads((directory / 'state.json').read_text())
    if state.get('version') != 1 or state.get('home') != str(home):
        raise InstallError('Резервная копия создана для другого пользователя или неизвестной версии установщика.')
    return state


def restore(directory, home, settings, uninstall_records=None, extra_file_origins=None):
    directory = directory.resolve()
    state = read_snapshot(directory, home)
    files = {name: (directory, entry) for name, entry in state['files'].items()}
    for name, origin in (extra_file_origins or {}).items():
        snapshot = Path(origin).resolve()
        roots = [(data / 'backups').resolve() for data in layout_pairs(home)[0]]
        if (not any(root.is_relative_to(home.resolve()) and snapshot.is_relative_to(root)
                    for root in roots) or name in files):
            raise InstallError('Недопустимый источник восстановления добавленного файла')
        added = read_snapshot(snapshot, home)
        if name not in added['files']:
            raise InstallError('Нет резервной записи для добавленного файла')
        files[name] = snapshot, added['files'][name]
    # Validate the entire snapshot before restoring any entry.
    for name, (snapshot, entry) in files.items():
        validate_destination(Path(name), home)
        if entry['kind'] == 'file':
            source = snapshot / entry['backup']
            if not source.resolve().is_relative_to(snapshot) or not source.is_file():
                raise InstallError('Недопустимый или отсутствующий резервный файл')
        elif entry['kind'] not in ('absent', 'symlink'):
            raise InstallError('Неизвестный тип резервной копии')
    for item in state['settings'] if uninstall_records is None else uninstall_records:
        validate_setting(item['schema'], item['key'])
    errors = []
    for name, (snapshot, entry) in reversed(list(files.items())):
        try:
            target = Path(name)
            if not target.is_absolute() or not target.is_relative_to(home):
                raise InstallError(f'Недопустимый путь в резервной копии: {target}')
            if entry['kind'] == 'file':
                source = snapshot / entry['backup']
                atomic_copy(source, target)
            elif entry['kind'] == 'symlink':
                temp = target.with_name(target.name + '.restore-' + uuid.uuid4().hex)
                try:
                    temp.symlink_to(entry['target'])
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
            elif entry['kind'] == 'absent':
                target.unlink(missing_ok=True)
            else:
                raise InstallError('Неизвестный тип резервной копии')
        except Exception as error:
            errors.append(str(error))
    records = state['settings'] if uninstall_records is None else uninstall_records
    installed_commands = {item['schema']: parse_variant(item['after']) for item in records
                          if item['key'] == 'command' and 'after' in item}
    owned_commands = {schema for schema, command in installed_commands.items()
                      if settings.get(schema, 'command') == command}
    for item in reversed(records):
        try:
            raw = item['raw']
            if uninstall_records is not None:
                # Remove our changes while retaining unrelated edits made since
                # installation. Explicit rollback/restore remains an exact restore.
                if 'after' not in item:
                    continue
                schema, key = item['schema'], item['key']
                current = settings.get(schema, key)
                before, after = parse_variant(raw), parse_variant(item['after'])
                if schema in installed_commands and schema not in owned_commands:
                    continue  # The user repurposed this shortcut.
                if isinstance(before, list) and isinstance(after, list):
                    added = [value for value in after if value not in before]
                    if schema == MEDIA and key == 'custom-keybindings':
                        added = [path for path in added if CUSTOM_SCHEMA + path in owned_commands]
                    current = [value for value in current if value not in added]
                    current.extend(value for value in before if value not in after and value not in current)
                    raw = variant(current)
                elif current != after:
                    continue
            if settings.get_raw(item['schema'], item['key']) != raw:
                settings.set_raw(item['schema'], item['key'], raw)
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise InstallError('Не удалось полностью восстановить: ' + '; '.join(errors))


def verify_bundle():
    # A Git checkout has no generated wheel/manifest artifacts. Dependencies
    # are then fetched by pip; release bundles always carry a manifest.
    if not (ROOT / 'MANIFEST.json').exists() and (ROOT / 'scripts/build_release.py').is_file():
        return
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    for name, expected in manifest.items():
        path = ROOT / name
        if not path.resolve().is_relative_to(ROOT) or not path.is_file():
            raise InstallError(f'Отсутствует файл комплекта: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise InstallError(f'Повреждён файл комплекта: {name}')


def preflight(settings):
    if sys.version_info < (3, 10):
        raise InstallError('Нужен Python 3.10 или новее.')
    if os.geteuid() == 0:
        raise InstallError('Запускайте ./install.sh от обычного пользователя, БЕЗ sudo. Пароль sudo будет запрошен только для системных пакетов.')
    if sys.platform != 'linux' or os.environ.get('XDG_SESSION_TYPE') != 'x11':
        raise InstallError('Нужен GNOME 43 / X11. Выберите сеанс Xorg/X11 при входе.')
    if not os.environ.get('DISPLAY') or not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        raise InstallError('Запустите установщик в терминале рабочего стола целевого пользователя, не через sudo/SSH.')
    version = output(['gnome-shell', '--version'])
    if not re.search(r'\b43(?:\.|\b)', version):
        raise InstallError(f'Поддерживается GNOME 43; обнаружено: {version}')
    output(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.DBus', '--object-path',
            '/org/freedesktop/DBus', '--method', 'org.freedesktop.DBus.GetNameOwner', 'org.gnome.Shell'])
    return version


def missing_packages():
    missing = []
    for name in APT_PACKAGES:
        result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', name], capture_output=True, text=True)
        if result.returncode or result.stdout.strip() != 'install ok installed':
            missing.append(name)
    return missing


def make_runtime(data, offline):
    runtimes = data / 'runtimes'
    if runtimes.is_symlink() or not runtimes.resolve().is_relative_to(data.resolve()):
        raise InstallError('Недопустимый каталог Python-окружений')
    runtimes.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtimes.chmod(0o700)
    runtime = runtimes / ('python-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    run_logged([sys.executable, '-m', 'venv', runtime], 'Создание Python-окружения')
    python = runtime / 'bin/python'
    pip = [python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install']
    local = run_logged(pip + ['--no-input', '--no-index', '--find-links', ROOT / 'wheels', '-r', ROOT / 'requirements.txt'],
                       'Установка Python-пакетов', check=False)
    if local.returncode:
        if offline:
            raise InstallError(f'В комплекте нет совместимых wheels для Python {platform.python_version()}/{platform.machine()}. Старый переключатель не изменён.')
        UI.note('Python-пакеты: загрузка из PyPI')
        run_logged(pip + ['--no-input', '--find-links', ROOT / 'wheels', '-r', ROOT / 'requirements.txt'], 'Загрузка Python-пакетов')
    run_logged([python, '-m', 'pip', '--disable-pip-version-check', 'check'], 'Проверка зависимостей')
    run_logged([python, '-c', 'import pyperclip; from Xlib.display import Display; Display().close()'], 'Проверка X11')
    (runtime / '.lang-switcher-runtime').write_text('lang-switcher\n')
    return runtime


def install(settings, home, offline, repository=None):
    release_version = (ROOT / 'VERSION').read_text().strip()
    UI.title(release_version)
    UI.step(1, 'Проверка окружения')
    version = preflight(settings)
    layout_plan(home)
    if UI.verbose:
        UI.note(f'{version} / X11')
    for target in managed_files(home):
        validate_destination(target, home)
    settings_plan(settings, home)  # Detect conflicts before installing anything.
    missing = missing_packages()
    if missing and offline:
        raise InstallError('Для offline-установки заранее установите системные пакеты: ' + ' '.join(missing))
    if missing:
        UI.note('Системные пакеты: ' + ' '.join(missing))
        execute(['sudo', '-v'])  # Keep the password prompt attached to the terminal.
    UI.step(2, 'Зависимости')
    if missing:
        run_logged(['sudo', '-n', 'apt-get', 'update'], 'Обновление списка пакетов')
        run_logged(['sudo', '-n', 'apt-get', 'install', '-y', *missing], 'Установка системных пакетов')
    command = home / '.local/bin/lang-switcher'
    if command.exists() and 'lang-switcher managed command' not in command.read_text():
        raise InstallError(f'Команда {command} уже существует и не принадлежит установщику.')
    migrate_layout(home)
    data = home / '.local/share/lang-switcher'
    runtime = make_runtime(data, offline)
    # Re-read the settings after package setup and before creating the snapshot.
    plan = settings_plan(settings, home)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    tx = Transaction(home, settings, data / 'backups' / stamp)
    app = home / '.local/lib/lang-switcher'
    extension = home / '.local/share/gnome-shell/extensions' / EXTENSION_UUID
    module_changed = extension_changed(extension)
    UI.step(3, 'Файлы и горячие клавиши')
    try:
        previous = read_config(home)
        tx.copy(ROOT / 'payload/switcher.py', app / 'switcher.py', 0o755)
        tx.copy(ROOT / 'payload/keyboard_x11.py', app / 'keyboard_x11.py', 0o644)
        tx.copy(ROOT / 'README.txt', app / 'INSTALL-README.txt', 0o644)
        for name in ('extension.js', 'metadata.json'):
            tx.copy(ROOT / 'payload/extension' / name, extension / name, 0o644)
            tx.copy(ROOT / 'payload/extension' / name, app / 'direct-gnome' / EXTENSION_UUID / name, 0o644)
        for mode in ('layout', 'case'):
            wrapper = home / '.local/bin' / f'switch_{mode}.sh'
            if wrapper.is_file():
                content = wrapper.read_text()
                if command_mode(content) == mode or '# lang-switcher legacy wrapper' in content:
                    tx.copy(ROOT / f'payload/switch_{mode}.sh', wrapper, 0o755)
        tx.copy(ROOT / 'install.py', data / 'manager.py', 0o644)
        tx.copy(ROOT / 'bootstrap.sh', data / 'bootstrap.sh', 0o755)
        tx.copy(ROOT / 'payload/lang-switcher', command, 0o755)
        tx.copy(ROOT / 'payload/lang-switcher.bash',
                home / '.local/share/bash-completion/completions/lang-switcher', 0o644)
        tx.link_runtime(runtime, data / 'current')
        # Changing sources may update MRU automatically. Include it in rollback.
        tx.remember_setting(INPUT, 'mru-sources')
        for schema, key, value in plan:
            tx.set(schema, key, value)
        config = {
            'version': release_version,
            'repository': repository or previous.get('repository'),
            'last_backup': str(tx.directory),
            'base_backup': str(Path(previous.get('base_backup', str(tx.directory))).resolve()),
            'last_log': str(UI.log_path) if UI.log_path is not None else None,
        }
        origins = {(item['schema'], item['key']): dict(item)
                   for item in previous.get('uninstall_settings', [])}
        for item in tx.state['settings']:
            if 'after' in item:
                key = item['schema'], item['key']
                if key in origins:
                    origins[key]['after'] = item['after']
                else:
                    origins[key] = dict(item)
        config['uninstall_settings'] = list(origins.values())
        tx.remember_file(data / 'install.json')
        # Files introduced by later versions need their own original snapshot:
        # the first installation's backup cannot contain those paths yet.
        base = Path(config['base_backup'])
        baseline = tx.state if base == tx.directory else read_snapshot(base, home)
        extra_files = {name: str(Path(origin).resolve())
                       for name, origin in previous.get('extra_file_origins', {}).items()}
        for name in tx.state['files']:
            if name not in baseline['files']:
                extra_files.setdefault(name, str(tx.directory))
        config['extra_file_origins'] = extra_files
        config_file = tx.directory / 'new-config.json'
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        tx.copy(config_file, data / 'install.json', 0o644)
        execute([runtime / 'bin/python', app / 'switcher.py', '--help'], stdout=subprocess.DEVNULL)
    except BaseException:
        UI.warn('Ошибка установки — возвращаю прежние файлы и настройки')
        restore(tx.directory, home, settings)
        raise
    installation_summary(module_changed or not module_active(), tx.directory)


def read_config(home):
    path = data_root(home) / 'install.json'
    return json.loads(path.read_text()) if path.exists() else {}


def repository_name(value):
    if value is not None and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise InstallError('Укажите GitHub-репозиторий в виде owner/lang-switcher.')
    return value


@contextmanager
def installation_lock(home):
    layout_plan(home)
    data = data_root(home)
    validate_destination(data / 'current', home)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data.stat().st_uid != os.getuid():
        raise InstallError('Каталог программы принадлежит другому пользователю')
    data.chmod(0o700)
    descriptor = os.open(data / '.install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                         os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError('Недопустимый файл блокировки установки')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError('Другая установка или восстановление уже выполняется.') from error
        yield


def extension_changed(extension):
    """Display-name edits do not require a running GNOME module to reload."""
    source = ROOT / 'payload/extension'
    try:
        if (extension / 'extension.js').read_bytes() != (source / 'extension.js').read_bytes():
            return True
        old = json.loads((extension / 'metadata.json').read_text())
        new = json.loads((source / 'metadata.json').read_text())
        return any(old.get(key) != new.get(key) for key in set(old) | set(new)
                   if key not in ('name', 'description'))
    except (OSError, ValueError):
        return True


def module_active():
    try:
        probe = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
                                '--object-path', '/org/gnome/Shell/Extensions/ZorinSwitcher',
                                '--method', 'org.gnome.Shell.Extensions.ZorinSwitcher.GetLayout'],
                               capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def show_status(home):
    config = read_config(home)
    UI.title(config.get('version'))
    if not config:
        UI.warn('Программа не установлена')
        return
    if module_active():
        UI.ok('Модуль GNOME активен')
    else:
        UI.warn('Модуль GNOME не загружен')
        UI.note('Английская раскладка → Alt+F2 → r → Enter')
    if UI.verbose:
        UI.line()
        UI.note('Репозиторий: ' + (config.get('repository') or 'не задан'))
        UI.note('Копия: ' + config['last_backup'])
        if config.get('last_log'):
            UI.note('Лог: ' + config['last_log'])


def show_commands(home):
    UI.title(read_config(home).get('version'))
    for command, description in (
        ('status', 'Состояние'), ('update', 'Обновить'),
        ('rollback', 'Вернуть предыдущую версию'), ('uninstall', 'Удалить'),
        ('restore КОПИЯ', 'Восстановить копию'),
        ('completion bash', 'Скрипт автодополнения'),
        ('layout / case', 'Обработать выделенный текст'),
    ):
        UI.note(f'{command:<18}{description}')
    UI.line()
    UI.note('--verbose  подробности и пути к журналам')


def uninstall(home, settings):
    config = read_config(home)
    if not config:
        raise InstallError('Нет сохранённой установки. Удалять неизвестные файлы не буду.')
    UI.title(config.get('version'))
    UI.note('Удаление и восстановление прежних настроек…')
    data = data_root(home)
    runtimes = data / 'runtimes'
    if runtimes.is_symlink() or not runtimes.resolve().is_relative_to(home.resolve()):
        raise InstallError('Отказ удалять Python-окружения за пределами HOME или через ссылку')
    # First installation's snapshot also restores an older manually installed
    # switcher, if one was present before this installer was used.
    restore(Path(config['base_backup']), home, settings,
            uninstall_records=config.get('uninstall_settings'),
            extra_file_origins=config.get('extra_file_origins'))
    current = (data / 'current').resolve()
    if runtimes.is_dir():
        for path in runtimes.iterdir():
            if (path.is_dir() and not path.is_symlink() and
                    re.fullmatch(r'python-\d{8}T\d{6}Z-[0-9a-f]{8}', path.name) and
                    path.resolve().parent == runtimes.resolve() and
                    not (path / '.lang-switcher-runtime').is_symlink() and
                    (path / '.lang-switcher-runtime').is_file() and
                    (path / '.lang-switcher-runtime').read_text().strip() == 'lang-switcher' and
                    path.resolve() != current):
                shutil.rmtree(path)
    UI.ok('Удалено · прежние файлы и настройки восстановлены')
    UI.note('Резервные копии сохранены')
    UI.note('При смене модуля: Alt+F2 → r → Enter (EN)')


def main(argv=None):
    parser = argparse.ArgumentParser(description='lang-switcher · GNOME 43 / X11. Запускать без sudo.')
    parser.add_argument('--offline', action='store_true', help='Не обращаться к сети; системные пакеты должны быть установлены')
    parser.add_argument('--repository', metavar='OWNER/REPO', help='GitHub-репозиторий для будущих обновлений')
    parser.add_argument('--verbose', action='store_true', help='Подробный вывод и пути к копии/журналу')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-only', action='store_true', help='Проверить совместимость и конфликты без изменений')
    action.add_argument('--restore', type=Path, metavar='BACKUP', help='Восстановить файлы и настройки из резервной копии установки')
    action.add_argument('--status', action='store_true', help='Состояние установленной версии')
    action.add_argument('--update', action='store_true', help='Установить последний GitHub Release')
    action.add_argument('--rollback', action='store_true', help='Откатить последнее обновление')
    action.add_argument('--uninstall', action='store_true', help='Удалить установленную версию, восстановив состояние до установки')
    action.add_argument('--commands', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    UI.verbose = args.verbose
    UI.log_path = None
    try:
        if os.geteuid() == 0:
            raise InstallError('Запускайте установщик от обычного пользователя, без sudo.')
        settings, home = Settings(), Path.home()
        repository = repository_name(args.repository)
        if args.commands:
            show_commands(home)
        elif args.status:
            show_status(home)
        elif args.update:
            repository = repository or read_config(home).get('repository') or DEFAULT_REPOSITORY
            repository_name(repository)
            command = ['bash', data_root(home) / 'bootstrap.sh', '--repo', repository]
            if args.verbose:
                command.append('--verbose')
            execute(command)
        elif args.uninstall:
            with installation_lock(home):
                uninstall(home, settings)
        elif args.restore or args.rollback:
            backup = args.restore or Path(read_config(home).get('last_backup', ''))
            if not args.restore and not read_config(home):
                raise InstallError('Нет сохранённой установки для отката.')
            UI.title()
            UI.note('Восстановление…')
            with installation_lock(home):
                restore(backup, home, settings)
            UI.ok('Предыдущая версия и настройки восстановлены')
            UI.note('При смене модуля: Alt+F2 → r → Enter (EN)')
        else:
            verify_bundle()
            if args.check_only:
                UI.title((ROOT / 'VERSION').read_text().strip())
                version = preflight(settings)
                layout_plan(home)
                settings_plan(settings, home)
                UI.ok(f'{version} / X11')
                UI.ok('Конфликтов горячих клавиш нет')
                missing = missing_packages()
                if missing:
                    UI.warn('Нужны пакеты: ' + ' '.join(missing))
                else:
                    UI.ok('Системные пакеты установлены')
                UI.note('Проверено · изменений нет')
            else:
                if repository is None and (ROOT / 'REPOSITORY').exists():
                    repository = repository_name((ROOT / 'REPOSITORY').read_text().strip() or None)
                repository = repository or DEFAULT_REPOSITORY
                with installation_lock(home):
                    with operation_log(home):
                        install(settings, home, args.offline, repository)
    except KeyboardInterrupt:
        UI.error('Операция прервана')
        return 130
    except Exception as error:
        UI.error(str(error))
        if UI.log_path is not None:
            print(f'  Лог: {UI.log_path}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
