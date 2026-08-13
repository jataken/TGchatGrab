# Регрессионные тесты конструктора ботов

Скрипты запускаются напрямую, без pytest — каждый печатает, что проверяет,
и падает с `AssertionError`, если поведение сломалось.

```
python tests/test_migration.py       # миграция схемы v2 -> v3 не теряет строки
python tests/test_repeat_contact.py  # повторный контакт не теряет заявку
python tests/test_templates.py       # подстановка переменных и выбор шаблона
python tests/test_scheduler.py       # напоминания по неактивности и расписание
python tests/test_step_ids.py        # id шагов сценария стабильны при правках
python tests/test_send_limits.py     # исходящие не уходят пачкой
python tests/test_bot_api_chat_type.py  # бот в группе не считает её личкой
python tests/test_bot_context.py     # выбор бота общий для блока
python tests/test_gaps.py            # разрывы в собранном считаются верно
python tests/test_duplicates.py      # повторы текста и их фильтрация
python tests/test_tray.py            # трей, уведомления, автозапуск
```

Все они пишут во временные каталоги в `/tmp` и не трогают рабочую базу.
