# Облачное демо и подготовка подачи

Выбран Render Free, workspaceподтверждён пользователем. Сервис Python, Frankfurt, веткаmain, сборка `python -m compileall -q cqa`, запуск `python -u -m cqa host`, порт изPORT, bind0.0.0.0, HTTPSoriginизRENDER_EXTERNAL_URL. Источник конфигурации — настройки только нового сервиса в Render. Auto-deployвыключен для управляемой демонстрации.

Код доступа передаётся жюри в заявке без регистрации. Каждый вход создаёт независимую SQLite/CSRF-область. CookieSecure/HttpOnly/SameSite=Lax, защитаHost/Origin, выход отзывает сеанс. Готовых оценок в сеансах нет: три собственных входа требуют новогоAI-вызова. Ключи только на сервере, посетитель может выбрать проверенную модель, но не заменяет серверные секреты. НужныCQA_DEMO_ACCESS_HASH, CQA_PROVIDER=gemini, CQA_MODEL=gemini-3.5-flash, CQA_TEAM_NAME=THE BOYS, новыеGEMINI_API_KEY/GROQ_API_KEY и подтверждения соответствующих бесплатных тарифов. Хеш доступа генерируется из случайного кода, самкод в Gitне публикуется.

Сеанс живётдо4часов в текущем процессе; лимит16одновременныхсеансов,1одновременныйAI-пакет на сервис, максимум10диалогов/пакет, загрузка2МБ,20запланированныхAI-диалогов/суткипроцесса. Внешние бесплатные квоты дополнительно ограничивают вызовы. Никакого платногоfallbackили замены результатовнет.

ОграниченияRenderFree: сон после15минбез трафика, первый вход после сна около1мин, потеря локальныхданных при сне/рестарте/деплое; общие месячные бесплатные лимитыworkspace. Данные необходимо скачать до выхода. ОфициальныйPROD обрабатывайте локально с сохранениемплана и результатов. На демо не загружены приватныйDEV, локальнаябаза или аудиоссылки. Функциональностьпроверяется на синтетическихданных. [Официальныеусловия](https://render.com/docs/free).

Для команды создаётся приватный пакет `.local/submission-pack/`:готовыйтекстзаявки, коддоступа, ссылка, DEVresults.json, примеры и инструкцияконтрольногочаса. Публичнаяинструкциядляжюри будетвsubmission/README.md. ФактическиеURL,commit,deployID, внешнийsmokeиCI — вevidence/deployment/. Покаliveнеподтверждён, его статуснеPASS.

## Локальный контроль с новыми ключами

Новые ключи из отдельнойформы8773находятсяв`.local/deployment/new-keys/provider.json`. Основное подключение ноутбуканеизменяется. Дляконтрольного набора:

```powershell
python -m cqa batch --config .local/deployment/new-keys/provider.json --dataset .local/prod.json --provider gemini --model gemini-3.5-flash --output .local/prod-predictions.json
python -m cqa batch --config .local/deployment/new-keys/provider.json --dataset .local/prod.json --provider gemini --model gemini-3.5-flash --output .local/prod-predictions.json --resume
python -m cqa build-submission --predictions .local/prod-predictions.json --dataset .local/prod.json --group prod --team 'THE BOYS'
python -m cqa check-submission --file results/prod/results.json --dataset .local/prod.json --group prod
```

Затемобычныйcommit/pushдо стоп-кода,см.docs/submission.md. До выдачи реальногоPROD файлне создаётся. Экспертнаяaccuracyнеизвестна; авторские тесты неявляютсяGT. Презентацияv3историческая; текущиесвидетельствахостингаи171тестдобавленывREADME.
