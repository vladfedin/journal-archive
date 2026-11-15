#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v systemd-run >/dev/null; then
  printf 'Для фонового запуска нужен systemd. Запустите ./start.sh в терминале.\n' >&2
  exit 1
fi

if ! systemctl --user is-active --quiet livejournal-exporter.service; then
  systemd-run --user --collect \
    --unit=livejournal-exporter \
    --description='Локальный архив Живого Журнала' \
    --working-directory="$project_dir" \
    --property=Restart=on-failure \
    --property=RestartSec=3 \
    "$project_dir/start.sh" serve --no-browser
fi

printf 'Адрес: http://127.0.0.1:8787\n'
printf 'Состояние: systemctl --user status livejournal-exporter\n'
printf 'Остановка: systemctl --user stop livejournal-exporter\n'
