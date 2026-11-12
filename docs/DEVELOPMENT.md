# Разработка и сборка

```bash
make test
make bundle REPOSITORY=AlexZhilov/lang-switcher
```

Сборка создаёт:

```text
dist/lang-switcher.tar.gz
dist/lang-switcher.tar.gz.sha256
```

Python wheels скачиваются в игнорируемую папку `wheels/`, в Git не попадают.
При уже подготовленном кеше можно собрать без сети:

```bash
python3 scripts/build_release.py --repo AlexZhilov/lang-switcher --no-download
```

`make test` не устанавливает программу и не меняет настройки рабочего стола:
проверки используют временные HOME и подставной backend GSettings. Проверяются
обновление старой установки, чужие сочетания, повторная установка, откат,
удаление и очистка временной папки bootstrap, включая ошибки загрузки.

В совместимом сеансе GNOME/X11 можно проверить Python-зависимости в одноразовом окружении,
без установки переключателя и без отправки нажатий:

```bash
python3 scripts/smoke_runtime.py
```

В `.github/workflows/` подготовлены тесты и публикация Release по тегу `v*`.
Перед публикацией выставьте `VERSION`, затем отправьте соответствующий Git-тег.
Workflow проверяет совпадение тега с VERSION, собирает комплект и прикладывает
его к GitHub Release. Источник команд Actions — официальные проекты
[checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python) и
[GitHub CLI](https://cli.github.com/manual/gh_release_create).

Исходный пользовательский переключатель был основан на модификации скрипта
Михаила Винакова. Эта версия сохраняет его поведение преобразования и добавляет
интеграцию GNOME, обработку ошибок и установку/обновление.
