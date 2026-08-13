# Расписание cron на raspberrypi

Фиксирует факт добавления регулярных задач в crontab на боевом Pi
(`/mnt/ssd/sergey_trade_bridge`), не дублирует сам crontab — актуальный
список всегда смотреть через `crontab -l` на Pi.

## weekly_live_report.py — еженедельный отчёт по живой автостратегии

**Добавлено:** 2026-08-13.

```
45 6 * * MON /usr/bin/flock -n /tmp/weekly_live_report.lock /bin/bash -lc "cd $PROJECT && set -a; source .env; set +a; $PY weekly_live_report.py --days 7 --push-sheets --notify-telegram --out $LOG/weekly_live_report_$(date +\%Y-\%m-\%d).md >> $LOG/weekly_live_report_cron.log 2>&1"
```

- **Когда:** понедельник, 06:45 МСК — за 25 минут до `universe_builder.py`
  (07:10, начало дневной цепочки), не пересекается ни с одной другой задачей.
- **Что делает:** собирает отчёт за последние 7 дней (воронка LLM-фильтра,
  ошибки исполнителя, комиссионная экономика, распределение размеров
  позиций), пишет полный текст в датированный файл
  `logs/weekly_live_report_YYYY-MM-DD.md` (предыдущие отчёты не
  перезатираются), загружает сводку и полный текст в Google Sheets
  (вкладки `WEEKLY_SUMMARY` и `WEEKLY_FULL` в таблице
  `GSHEETS_SPREADSHEET_ID` из `.env`), затем шлёт короткое уведомление
  в Telegram со ссылкой на таблицу (без дублирования текста отчёта).
- **Проверено вручную** перед постановкой на крон: реальный тестовый
  прогон 2026-08-13 с `--push-sheets --notify-telegram` — запись в обе
  вкладки и сообщение в Telegram подтверждены.
- Первый реальный крон-запуск — 2026-08-17 (понедельник).
