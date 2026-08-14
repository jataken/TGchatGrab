# Регрессионные тесты

Скрипты запускаются напрямую, без pytest — каждый печатает, что проверяет,
и падает с `AssertionError`, если поведение сломалось. Все пишут во
временные каталоги в `/tmp` и не трогают рабочую базу.

Их же гоняет CI на каждый пуш (`.github/workflows/tests.yml`), поэтому
новый тест достаточно положить сюда с именем `test_*.py` — подхватится сам.

```
QT_QPA_PLATFORM=offscreen python tests/smoke_screens.py   # все экраны открываются
python tests/test_migration.py         # миграция схемы v2 -> v3 не теряет строки
python tests/test_repeat_contact.py    # повторный контакт не теряет заявку
python tests/test_templates.py         # подстановка переменных и выбор шаблона
python tests/test_scheduler.py         # напоминания по неактивности и расписание
python tests/test_step_ids.py          # id шагов сценария стабильны при правках
python tests/test_send_limits.py       # исходящие не уходят пачкой
python tests/test_bot_api_chat_type.py # бот в группе не считает её личкой
python tests/test_bot_context.py       # выбор бота общий для блока
python tests/test_gaps.py              # разрывы в собранном считаются верно
python tests/test_duplicates.py        # повторы текста и их фильтрация
python tests/test_tray.py              # трей, уведомления, автозапуск
python tests/test_export_estimate.py   # оценка выгрузки лёгкая и точная
python tests/test_log_rotation.py      # журнал не растёт бесконечно
python tests/test_diagnostics.py       # диагностическая запись (временная, см. TEMPORARY.md)
```

Тестам с интерфейсом нужен `QT_QPA_PLATFORM=offscreen` и системные
библиотеки Qt (`libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3`).
