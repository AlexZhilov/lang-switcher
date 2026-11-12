lang-switcher — исправление раскладки и регистра выделенного текста

Требования: GNOME Shell 43, X11, Python 3.10+, система с APT/dpkg.

Установка от обычного пользователя, БЕЗ sudo:
  ./install.sh --check-only
  ./install.sh

После первой установки: в английской раскладке Alt+F2 → r → Enter.
Можно вместо этого выйти из учётной записи и войти снова.

Ctrl+Shift+Пробел — преобразовать текст RU/EN и выбрать язык ввода.
Ctrl+Shift+Q — переключить регистр выделенного текста.

Bash: команды и параметры дополняются по Tab.
Для текущего терминала: source <(lang-switcher completion bash)

Управление:
  lang-switcher status
  lang-switcher update
  lang-switcher rollback
  lang-switcher uninstall

Полная инструкция: README.md.
Дополнительная справка: docs/REFERENCE.md.
Проект: https://github.com/AlexZhilov/lang-switcher
