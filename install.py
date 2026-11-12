#!/usr/bin/env python3
"""Per-user installation of Zorin Switcher; sudo is used only for APT."""
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
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
EXTENSION_UUID = 'lang-switcher@local'
MEDIA = 'org.gnome.settings-daemon.plugins.media-keys'
CUSTOM_SCHEMA = MEDIA + '.custom-keybinding:'
CUSTOM_ROOT = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
INPUT = 'org.gnome.desktop.input-sources'
SHELL = 'org.gnome.shell'
APT_PACKAGES = ('python3-venv', 'xclip', 'libglib2.0-bin')


class InstallError(RuntimeError):
    pass


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
        execute(['gsettings', 'set', schema, key, raw])


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
    if 'zorin_switcher.py' in names:
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
    app, data = home / '.local/bin/zorin-switcher', home / '.local/share/zorin-switcher'
    extension = home / '.local/share/gnome-shell/extensions' / EXTENSION_UUID
    return {
        app / 'zorin_switcher.py', app / 'keyboard_x11.py', app / 'INSTALL-README.txt',
        app / 'direct-gnome' / EXTENSION_UUID / 'extension.js',
        app / 'direct-gnome' / EXTENSION_UUID / 'metadata.json',
        extension / 'extension.js', extension / 'metadata.json',
        home / '.local/bin/switch_layout.sh', home / '.local/bin/switch_case.sh',
        home / '.local/bin/lang-switcher', data / 'manager.py', data / 'bootstrap.sh',
        data / 'current', data / 'install.json',
    }


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
            path = CUSTOM_ROOT + f'zorin-switcher-{mode}/'
            index = 1
            while path in records or path in active:
                path = CUSTOM_ROOT + f'zorin-switcher-{mode}-{index}/'
                index += 1
            active.append(path)
        schema = CUSTOM_SCHEMA + path
        plan.extend((schema, key, value) for key, value in (
            ('name', 'Switch Layout' if mode == 'layout' else 'Switch Case'),
            ('command', shlex.quote(str(home / '.local/bin' / f'switch_{mode}.sh'))),
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


def restore(directory, home, settings, uninstall_records=None):
    directory = directory.resolve()
    state = json.loads((directory / 'state.json').read_text())
    if state.get('version') != 1 or state.get('home') != str(home):
        raise InstallError('Резервная копия создана для другого пользователя или неизвестной версии установщика.')
    # Validate the entire snapshot before restoring any entry.
    for name in state['files']:
        validate_destination(Path(name), home)
    for item in state['settings'] if uninstall_records is None else uninstall_records:
        validate_setting(item['schema'], item['key'])
    errors = []
    for name, entry in reversed(list(state['files'].items())):
        try:
            target = Path(name)
            if not target.is_absolute() or not target.is_relative_to(home):
                raise InstallError(f'Недопустимый путь в резервной копии: {target}')
            if entry['kind'] == 'file':
                source = directory / entry['backup']
                if not source.resolve().is_relative_to(directory):
                    raise InstallError('Недопустимый путь к резервному файлу')
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
        raise InstallError('Эта версия рассчитана на GNOME 43 в сеансе X11. Выберите Xorg/X11 при входе в Zorin.')
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
    execute([sys.executable, '-m', 'venv', runtime])
    python = runtime / 'bin/python'
    pip = [python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install']
    local = subprocess.run([str(arg) for arg in pip + ['--no-index', '--find-links', ROOT / 'wheels', '-r', ROOT / 'requirements.txt']])
    if local.returncode:
        if offline:
            raise InstallError(f'В комплекте нет совместимых wheels для Python {platform.python_version()}/{platform.machine()}. Старый переключатель не изменён.')
        print('Нужна совместимая сборка Python-зависимостей; загружаю из PyPI.', flush=True)
        execute(pip + ['--find-links', ROOT / 'wheels', '-r', ROOT / 'requirements.txt'])
    execute([python, '-m', 'pip', '--disable-pip-version-check', 'check'])
    execute([python, '-c', 'import pyperclip; from Xlib.display import Display; Display().close(); print("Python-зависимости и подключение к X11: OK")'])
    (runtime / '.lang-switcher-runtime').write_text('lang-switcher\n')
    return runtime


def install(settings, home, offline, repository=None):
    version = preflight(settings)
    for target in managed_files(home):
        validate_destination(target, home)
    settings_plan(settings, home)  # Detect conflicts before installing anything.
    missing = missing_packages()
    if missing and offline:
        raise InstallError('Для offline-установки заранее установите системные пакеты: ' + ' '.join(missing))
    if missing:
        print('Нужны системные пакеты:', ' '.join(missing), flush=True)
        execute(['sudo', 'apt-get', 'update'])
        execute(['sudo', 'apt-get', 'install', '-y', *missing])
    data = home / '.local/share/zorin-switcher'
    command = home / '.local/bin/lang-switcher'
    if command.exists() and 'lang-switcher managed command' not in command.read_text():
        raise InstallError(f'Команда {command} уже существует и не принадлежит установщику.')
    print('Подготовка изолированного Python-окружения…', flush=True)
    runtime = make_runtime(data, offline)
    # Re-read the settings after package setup and before creating the snapshot.
    plan = settings_plan(settings, home)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    tx = Transaction(home, settings, data / 'backups' / stamp)
    app = home / '.local/bin/zorin-switcher'
    extension = home / '.local/share/gnome-shell/extensions' / EXTENSION_UUID
    module_changed = any(not (extension / name).is_file() or
                         (extension / name).read_bytes() != (ROOT / 'payload/extension' / name).read_bytes()
                         for name in ('extension.js', 'metadata.json'))
    try:
        previous = read_config(home)
        tx.copy(ROOT / 'payload/zorin_switcher.py', app / 'zorin_switcher.py', 0o755)
        tx.copy(ROOT / 'payload/keyboard_x11.py', app / 'keyboard_x11.py', 0o644)
        tx.copy(ROOT / 'README.txt', app / 'INSTALL-README.txt', 0o644)
        for name in ('extension.js', 'metadata.json'):
            tx.copy(ROOT / 'payload/extension' / name, extension / name, 0o644)
            tx.copy(ROOT / 'payload/extension' / name, app / 'direct-gnome' / EXTENSION_UUID / name, 0o644)
        for mode in ('layout', 'case'):
            tx.copy(ROOT / f'payload/switch_{mode}.sh', home / '.local/bin' / f'switch_{mode}.sh', 0o755)
        tx.copy(ROOT / 'install.py', data / 'manager.py', 0o644)
        tx.copy(ROOT / 'bootstrap.sh', data / 'bootstrap.sh', 0o755)
        tx.copy(ROOT / 'payload/lang-switcher', command, 0o755)
        tx.link_runtime(runtime, data / 'current')
        # Changing sources may update MRU automatically. Include it in rollback.
        tx.remember_setting(INPUT, 'mru-sources')
        for schema, key, value in plan:
            tx.set(schema, key, value)
        config = {
            'version': (ROOT / 'VERSION').read_text().strip(),
            'repository': repository or previous.get('repository'),
            'last_backup': str(tx.directory),
            'base_backup': previous.get('base_backup', str(tx.directory)),
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
        config_file = tx.directory / 'new-config.json'
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        tx.copy(config_file, data / 'install.json', 0o644)
        execute([runtime / 'bin/python', app / 'zorin_switcher.py', '--help'], stdout=subprocess.DEVNULL)
    except BaseException:
        print(f'Ошибка установки. Восстанавливаю предыдущие файлы и настройки из {tx.directory}', file=sys.stderr, flush=True)
        restore(tx.directory, home, settings)
        raise
    probe = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
                            '--object-path', '/org/gnome/Shell/Extensions/ZorinSwitcher',
                            '--method', 'org.gnome.Shell.Extensions.ZorinSwitcher.GetLayout'],
                           capture_output=True, text=True, timeout=3)
    print(f'\nУстановка завершена: {version}.\nРезервная копия: {tx.directory}')
    print('Ctrl+Shift+Пробел — раскладка текста и язык; Ctrl+Shift+Q — регистр.')
    if module_changed or probe.returncode:
        print('\nНужно загрузить модуль GNOME. В английской раскладке нажмите Alt+F2, введите r и Enter.\nЛибо выйдите из учётной записи и войдите снова. До этого новая смена раскладки не заработает.')
    else:
        print('Модуль GNOME уже загружен. Можно пользоваться, перезапуск не требуется.')
    print('Команды: lang-switcher status | update | rollback | uninstall')
    print(f'Ручное восстановление: ./install.sh --restore {shlex.quote(str(tx.directory))}')


def read_config(home):
    path = home / '.local/share/zorin-switcher/install.json'
    return json.loads(path.read_text()) if path.exists() else {}


def repository_name(value):
    if value is not None and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise InstallError('Укажите GitHub-репозиторий в виде owner/lang-switcher.')
    return value


@contextmanager
def installation_lock(home):
    data = home / '.local/share/zorin-switcher'
    validate_destination(data / 'current', home)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data.stat().st_uid != os.getuid():
        raise InstallError('Каталог программы принадлежит другому пользователю')
    data.chmod(0o700)
    with (data / '.install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError('Другая установка или восстановление уже выполняется.') from error
        yield


def show_status(home):
    config = read_config(home)
    if not config:
        print('lang-switcher не установлен этим установщиком.')
        return
    print('lang-switcher ' + config['version'])
    print('Репозиторий:', config.get('repository') or 'не задан')
    print('Последняя резервная копия:', config['last_backup'])
    probe = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
                            '--object-path', '/org/gnome/Shell/Extensions/ZorinSwitcher',
                            '--method', 'org.gnome.Shell.Extensions.ZorinSwitcher.GetLayout'],
                           capture_output=True, text=True, timeout=3)
    print('Модуль GNOME:', 'загружен' if probe.returncode == 0 else 'не загружен; нужен Alt+F2 → r → Enter')


def uninstall(home, settings):
    config = read_config(home)
    if not config:
        raise InstallError('Нет сохранённой установки. Удалять неизвестные файлы не буду.')
    data = home / '.local/share/zorin-switcher'
    runtimes = data / 'runtimes'
    if runtimes.is_symlink() or not runtimes.resolve().is_relative_to(home.resolve()):
        raise InstallError('Отказ удалять Python-окружения за пределами HOME или через ссылку')
    # First installation's snapshot also restores an older manually installed
    # switcher, if one was present before this installer was used.
    restore(Path(config['base_backup']), home, settings,
            uninstall_records=config.get('uninstall_settings'))
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
    print('Установленная версия удалена. Файлы и настройки до первой установки восстановлены.')
    print('Резервные копии сохранены. Общие системные пакеты APT не удалялись.')
    print('Если восстановлена другая версия модуля GNOME, перезапустите оболочку: Alt+F2 → r → Enter.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Установка/обновление Zorin Switcher для GNOME 43/X11. Запускать БЕЗ sudo.')
    parser.add_argument('--offline', action='store_true', help='Не обращаться к сети; системные пакеты должны быть установлены')
    parser.add_argument('--repository', metavar='OWNER/REPO', help='GitHub-репозиторий для будущих обновлений')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-only', action='store_true', help='Проверить совместимость и конфликты без изменений')
    action.add_argument('--restore', type=Path, metavar='BACKUP', help='Восстановить файлы и настройки из резервной копии установки')
    action.add_argument('--status', action='store_true', help='Состояние установленной версии')
    action.add_argument('--update', action='store_true', help='Установить последний GitHub Release')
    action.add_argument('--rollback', action='store_true', help='Откатить последнее обновление')
    action.add_argument('--uninstall', action='store_true', help='Удалить установленную версию, восстановив состояние до установки')
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise InstallError('Запускайте установщик от обычного пользователя, без sudo.')
        settings, home = Settings(), Path.home()
        repository = repository_name(args.repository)
        if args.status:
            show_status(home)
        elif args.update:
            repository = repository or read_config(home).get('repository')
            if not repository:
                raise InstallError('Репозиторий ещё не задан. Используйте lang-switcher update --repository OWNER/REPO.')
            repository_name(repository)
            execute(['bash', home / '.local/share/zorin-switcher/bootstrap.sh', '--repo', repository])
        elif args.uninstall:
            with installation_lock(home):
                uninstall(home, settings)
        elif args.restore or args.rollback:
            backup = args.restore or Path(read_config(home).get('last_backup', ''))
            if not args.restore and not read_config(home):
                raise InstallError('Нет сохранённой установки для отката.')
            with installation_lock(home):
                restore(backup, home, settings)
            print('Предыдущие файлы и настройки восстановлены. Если менялась версия модуля GNOME, перезапустите оболочку через Alt+F2 → r → Enter.')
        else:
            verify_bundle()
            if args.check_only:
                version = preflight(settings)
                settings_plan(settings, home)
                print(f'Совместимость: {version}, X11 — OK. Конфликтов горячих клавиш нет.')
                missing = missing_packages()
                print('Нужна установка системных пакетов: ' + ' '.join(missing) if missing else 'Системные пакеты установлены.')
                print('Проверка завершена. Файлы, настройки и зависимости не изменялись.')
            else:
                if repository is None and (ROOT / 'REPOSITORY').exists():
                    repository = repository_name((ROOT / 'REPOSITORY').read_text().strip() or None)
                with installation_lock(home):
                    install(settings, home, args.offline, repository)
    except KeyboardInterrupt:
        print('\nУстановка прервана.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Установка не завершена: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
