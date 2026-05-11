# Синхронізація Monobank (чорна карта) → Firefly III — дизайн

Дата: 2026-05-12
Статус: затверджено до переходу в план реалізації

## 1. Контекст і мета

У користувача на Raspberry Pi 4 (aarch64) у Portainer уже працює Firefly III. Потрібен у Firefly III asset-рахунок, повністю синхронізований із чорною карткою Monobank: усі операції автоматично потрапляють у Firefly, а вся доступна історія завантажується одноразово.

Pi доступний лише в локальній мережі — публічного HTTPS немає, тож webhook від Monobank використати не можна. Синхронізація реалізується опитуванням Monobank API за розкладом.

## 2. Узгоджені рішення

- **Власний сервіс на Python** у власному Docker-контейнері; розгортається окремим стеком у Portainer на тій самій Docker-мережі, що й Firefly III. Контейнер **не відкриває портів** — лише вихідні запити: HTTPS до `api.monobank.ua` і HTTP до контейнера Firefly всередині мережі.
- **Polling, не webhook.** Інтервал інкрементальної синхронізації — **5 хв** (технічний мінімум через ліміти Monobank — 60 с; 5 хв — спокійний запас).
- **Повний бекфіл історії** при першому запуску, з «підлогою» ≈ 3 роки (`BACKFILL_FLOOR_DATE`, дефолт `2023-05-01`).
- **Один спільний рахунок-контрагент «Monobank»** у Firefly для всіх отримувачів/відправників; назва магазину/контрагента йде в опис транзакції (не плодимо expense/revenue-рахунки на кожен мерчант).
- **MCC без автокатегоризації за замовчуванням**: код MCC кладеться в нотатки транзакції + тег `monobank`, далі категоризація — правилами самого Firefly. Опційно — невелика вбудована таблиця MCC→категорія, вимкнена прапорцем (дефолт off).
- **Усі рахунки з `type: "black"`** із `client-info` синхронізуються — кожен окремим asset-рахунком у Firefly (на випадок мультивалютної чорної карти UAH/USD/EUR). Якщо рахунок один — він один.
- **Розгортання**: код у GitHub-репозиторії `github.com/Vatsonio/mono_sync` → GitHub Actions білдить образ (`linux/arm64` + `linux/amd64`) → пушить у GHCR (`ghcr.io/vatsonio/mono_sync`) → у Portainer «Add stack» з `docker-compose.yml` + env-змінні. Образ без секретів → пакет GHCR публічний (Pi тягне без авторизації).

## 3. Архітектура

Один контейнер `monobank-firefly-sync`, база `python:3.12-slim`. Усередині — нескінченний процес: разовий бекфіл (якщо не завершений) → потім цикл інкрементальної синхронізації кожні `POLL_INTERVAL_MINUTES`.

Модулі (`src/mono_sync/`):

- `config.py` — читання конфігурації з env, валідація.
- `monobank.py` — клієнт Monobank API; усередині **глобальний rate-limiter: ≤ 1 запит до Monobank на 65 с**; обробка `429`/5xx/мережевих помилок з бекофом.
- `firefly.py` — клієнт Firefly III API: знайти/створити asset-рахунок, пошук транзакції за `external_id`, `POST`/`PUT` транзакції.
- `store.py` — стан у SQLite на volume `/data/state.db`.
- `mapping.py` — **чиста функція** `StatementItem → тіло транзакції Firefly` (знак суми, валюта, FX, MCC, hold, нотатки). Головна ціль юніт-тестів.
- `sync.py` — оркестрація: `backfill()` та `incremental_cycle()`.
- `__main__.py` — точка входу.

## 4. Зовнішні інтерфейси

### 4.1. Monobank personal API (`https://api.monobank.ua`)

- Авторизація: заголовок `X-Token: <personal token>` (видається в особистому кабінеті `https://api.monobank.ua/`; не протухає, доки не відкликаний у застосунку).
- `GET /personal/client-info` — ліміт **1 запит / 60 с**. Повертає `name`, `accounts[]`, `jars[]`. Поля рахунку: `id`, `sendId`, `balance` (мінорні одиниці), `creditLimit`, `type` (`black`, `white`, `platinum`, `fop`, …), `currencyCode` (ISO 4217 числовий: 980=UAH, 840=USD, 978=EUR), `cashbackType`, `maskedPan[]`, `iban`.
- `GET /personal/statement/{account}/{from}/{to}` — ліміт **1 запит / 60 с**; максимальне вікно — **31 день + 1 год**; повертає **до 500** елементів. `{account}` — `id` рахунку (або `0` для дефолтного). `{from}`/`{to}` — Unix-секунди.
- Елемент виписки (`StatementItem`): `id` (унікальний ідентифікатор операції), `time` (Unix), `description`, `mcc`, `originalMcc`, `hold` (bool — сума ще не списана остаточно), `amount` (мінорні одиниці у валюті рахунку, **знаковий**: <0 — витрата), `operationAmount` (мінорні одиниці у валюті операції), `currencyCode` (валюта операції), `commissionRate`, `cashbackAmount`, `balance` (залишок рахунку **після** операції, мінорні одиниці), `comment?`, `receiptId?`, `invoiceId?`, `counterEdrpou?`, `counterIban?`, `counterName?`.

Обмеження: усі звернення до Monobank проходять через єдиний rate-limiter (≤ 1 / 65 с), щоб гарантовано не впертись у ліміти навіть при перемиканні між `client-info` і `statement`.

### 4.2. Firefly III API

- База: `${FIREFLY_URL}/api/v1` (внутрішнє ім'я контейнера Firefly у Docker-мережі, напр. `http://app:8080`).
- Авторизація: `Authorization: Bearer <Personal Access Token>` (Firefly → Options → Profile → OAuth → Create new token), `Accept: application/json`.
- `GET /api/v1/accounts?type=asset` — знайти існуючий asset-рахунок карти (за збереженим id або за назвою).
- `POST /api/v1/accounts` — створити asset-рахунок: `name`, `type=asset`, `account_role=defaultAsset`, `currency_code`, `opening_balance`, `opening_balance_date`, `notes`.
- `GET /api/v1/search/transactions?query=external_id:"<mono id>"` — підстрахувальний пошук для дедупу (якщо SQLite втрачено).
- `POST /api/v1/transactions` — створити транзакцію (тіло — масив `transactions` з одним елементом; `error_if_duplicate_hash=false`, `apply_rules=true`, `fire_webhooks=false`).
- `PUT /api/v1/transactions/{id}` — оновити (коли операція Monobank змінилась: hold→списано, інша сума).
- FX: якщо `currencyCode` операції ≠ валюти asset-рахунку — заповнюємо `foreign_amount` = `operationAmount/100` і `foreign_currency_code` = ISO-код `currencyCode`.

## 5. Потік даних

### 5.1. Бекфіл (разово, при першому запуску)

1. `GET /personal/client-info` → відібрати рахунки з `type == "black"`; для кожного — знайти/створити asset-рахунок у Firefly, записати мапінг `mono_account_id → firefly_account_id` у store.
2. Для кожного black-рахунку: йти вікнами по 31 день назад від `now`: `[now−31д, now]`, `[now−62д, now−31д]`, … Пауза між будь-якими викликами Monobank — через rate-limiter (≥ 65 с).
3. Кожну операцію вікна — `upsert` у Firefly з `external_id = StatementItem.id` (див. §6).
4. Якщо вікно повернуло **рівно 500** елементів — поділити це вікно навпіл і перезапросити обидві половини (рекурсивно), щоб не загубити операції.
5. Зупинка для рахунку: **два вікна поспіль порожні** АБО досягли `BACKFILL_FLOOR_DATE`.
6. Курсор бекфілу (`backfill_cursor` на рахунок) персиститься у store після кожного вікна → перезапуск контейнера продовжує з того ж місця.
7. Після обробки найстарішої отриманої операції рахунку — виставити `opening_balance` asset-рахунку = `(balance найстарішої операції − amount найстарішої операції) / 100`, з `opening_balance_date` = дата найстарішої операції мінус 1 день. Тоді порахований Firefly баланс збігатиметься з полем `balance` Monobank у кожній точці історії.
8. Позначити `backfill_complete=true` (на рахунок і глобально).

Орієнтовна тривалість: ≈ 1 виклик/хв × кількість 31-денних вікон у 3 роках ≈ 36 викликів ≈ 35–45 хв на рахунок. Разово, у фоні, резюмабельно.

### 5.2. Інкрементальна синхронізація (кожні `POLL_INTERVAL_MINUTES`)

1. Для кожного black-рахунку: `GET /personal/statement/{account}/{last_synced_time − 24год}/{now}` — нахльост ~доба, щоб підхопити операції, що «доїхали» із запізненням, та оновлення hold→списано.
2. Кожна операція: якщо `mono_tx_id` немає у store → `POST` нова транзакція; якщо є, але хеш операції змінився (сума/hold інші) → `PUT` оновити транзакцію у Firefly.
3. `last_synced_time` рахунку ← максимальний `time` оброблених операцій.
4. Раз на годину — `GET /personal/client-info`, звірити `balance` Monobank із порахованим балансом Firefly (`GET /api/v1/accounts/{id}`); при розходженні — `WARNING` у лог (історію Monobank могло бути усічено, або hold-операція змінила суму поза вікном нахльосту).
5. Структурований лог у stdout (видно в Portainer): «синхронізовано N нових, оновлено M, помилок K; баланс Mono=X / Firefly=Y».

## 6. Відображення `StatementItem` → транзакція Firefly

- `amount < 0` → `type = "withdrawal"`: `source_id` = asset-рахунок карти, `destination_name = "Monobank"`.
- `amount > 0` → `type = "deposit"`: `destination_id` = asset-рахунок карти, `source_name = "Monobank"`.
- `amount`: `abs(amount) / 100`, `currency_code` = валюта asset-рахунку.
- `description` транзакції = `counterName` якщо непорожнє, інакше `description` від Monobank (назва мерчанта/контрагента).
- `date` = `time` (Unix) → ISO-8601 у зоні `Europe/Kyiv` (`TZ` env).
- `external_id` = `StatementItem.id`.
- `tags` = `["monobank"]` (+ `["monobank", "hold"]` якщо `hold == true`).
- `notes` — багаторядково: `MCC <mcc>`; `cashback <cashbackAmount/100>` якщо `> 0`; `hold` якщо `true`; `comment` якщо є; посилання на чек, якщо є `receiptId` (`https://check.gov.ua/...`).
- FX: якщо `currencyCode != валюта рахунку` → `foreign_amount = abs(operationAmount)/100`, `foreign_currency_code` = ISO-літерний код `currencyCode`.
- (Опційно, якщо `MCC_CATEGORIES=true`) `category_name` за вбудованою таблицею MCC→категорія; за замовчуванням не заповнюється.
- Хеш операції для виявлення змін: `sha1(f"{amount}|{hold}|{description}|{time}")` — зберігається у store.

Краєві випадки:
- **Hold.** Імпортуємо як є з тегом/нотаткою `hold`. Пізніша синхронізація з тим самим `id` та іншим хешем → `PUT`.
- **Кешбек.** Лише нотатка; окрему транзакцію не створюємо (Monobank часом проводить кешбек власною операцією — щоб не дублювати).
- **Перекази між власними рахунками / поповнення «банок».** Синхронізуємо лише чорну карту → другий бік недоступний → усе як `withdrawal`/`deposit`. (Майбутнє розширення — синхронізація jars та парування переказів у Firefly `transfer`.)
- **Мультивалютна чорна карта.** Кілька рахунків `type: "black"` → кожен окремим asset-рахунком; FX-логіка вище покриває покупки в інших валютах.

## 7. Стан (SQLite, `/data/state.db`)

- `accounts(mono_account_id PK, firefly_account_id, currency_code, last_synced_time, backfill_cursor, backfill_complete)`
- `transactions(mono_tx_id PK, mono_account_id, firefly_tx_id, time, hash, status)` — `status ∈ {ok, failed}`.
- `sync_state(key PK, value)` — напр. `backfill_complete`.

Перед створенням транзакції: перевірити `transactions` у store; якщо немає — додатково шукати у Firefly за `external_id` (підстраховка від втрати SQLite) і, якщо знайдено, записати мапінг без створення дубля.

## 8. Обробка помилок

- Monobank `429` / 5xx / мережа → бекоф і повтор (rate-limiter і так тримає ≥ 65 с між викликами).
- Monobank `403` (токен відкликано/невалідний) → гучний `ERROR` у лог, далі рідкі повтори.
- Firefly помилка валідації окремої транзакції → `WARNING` у лог, `status=failed` у store, рух далі; такі повторюються наступним циклом.
- Перезапуск/краш контейнера → стан у SQLite: бекфіл продовжується з `backfill_cursor`, інкремент — з `last_synced_time`.
- `restart: unless-stopped` у compose.

## 9. Розгортання

### 9.1. Артефакти в репозиторії

```
mono_sync/
  README.md
  Dockerfile                 # python:3.12-slim, requirements, COPY src, ENTRYPOINT ["python","-m","mono_sync"]
  requirements.txt           # requests (решта — стандартна бібліотека)
  docker-compose.yml         # для Portainer "Add stack"
  .env.example
  .github/workflows/build.yml
  src/mono_sync/  __main__.py config.py monobank.py firefly.py store.py mapping.py sync.py
  tests/  test_mapping.py test_store.py test_sync_backfill.py test_sync_incremental.py
  docs/superpowers/specs/2026-05-12-monobank-firefly-sync-design.md
```

### 9.2. GitHub Actions (`build.yml`)

On push до `main`: `docker/setup-qemu-action` + `docker/setup-buildx-action` + `docker/login-action` (GHCR, `GITHUB_TOKEN`) + `docker/build-push-action` → `platforms: linux/arm64,linux/amd64` → теги `ghcr.io/vatsonio/mono_sync:latest` і `:${{ github.sha }}`. Зробити GHCR-пакет публічним.

### 9.3. `docker-compose.yml` (Portainer stack)

```yaml
services:
  monobank-sync:
    image: ghcr.io/vatsonio/mono_sync:latest
    container_name: monobank-firefly-sync
    restart: unless-stopped
    environment:
      MONOBANK_TOKEN: ${MONOBANK_TOKEN}
      FIREFLY_URL: ${FIREFLY_URL}            # напр. http://app:8080
      FIREFLY_TOKEN: ${FIREFLY_TOKEN}
      POLL_INTERVAL_MINUTES: "5"
      BACKFILL: "true"
      BACKFILL_FLOOR_DATE: "2023-05-01"
      MCC_CATEGORIES: "false"
      TZ: "Europe/Kyiv"
      LOG_LEVEL: "info"
    volumes:
      - monobank-sync-data:/data
    networks:
      - firefly
volumes:
  monobank-sync-data:
networks:
  firefly:
    external: true
    name: <ім'я Docker-мережі стека Firefly>
```

Без `ports:`. Точні `FIREFLY_URL` (ім'я+порт контейнера Firefly) та ім'я Docker-мережі беруться з наявного стека Firefly під час впровадження. У Portainer токени вписуються в секції Environment variables (не в git).

### 9.4. Оновлення

Push у GitHub → Actions перебілдив → у Portainer на стеку «Pull and redeploy». (Опційно — Portainer webhook або Watchtower для автооновлення; необов'язково.)

## 10. Тести

- **Юніт**: `mapping.py` (знак суми → withdrawal/deposit; `amount/100`; FX `foreign_amount`/`foreign_currency_code`; MCC у нотатках; hold-тег; `counterName` vs `description`; хеш), `store.py` (upsert/дедуп; виявлення зміни хешу; курсори).
- **Інтеграційні з фейками** (без мережі): фейковий Monobank-клієнт (віддає підготовлені виписки + симулює ліміт/порожні вікна/«рівно 500»), фейковий Firefly-клієнт (приймає POST/PUT, віддає id), SQLite у пам'яті. Перевіряємо: бекфіл (хід вікнами, рекурсивне розбиття на 500, курсор/резюмабельність, зупинка на 2 порожніх / на підлозі, виставлення opening_balance), інкремент (нахльост 24 год, оновлення hold→списано, дедуп при повторі).

## 11. Поза обсягом (YAGNI / можливі майбутні розширення)

- Webhook (миттєва синхронізація) — якщо з'явиться публічний HTTPS (Cloudflare Tunnel / Tailscale Funnel).
- Синхронізація «банок» (jars) та інших (не-black) рахунків.
- Розпізнавання переказів між власними рахунками як Firefly `transfer`.
- Окремі транзакції під кешбек.
- Авто-категоризація MCC за замовчуванням.
- Web/healthz-ендпоінт (наразі здоров'я видно зі статусу контейнера й логів у Portainer).

## 12. Відкриті питання для етапу впровадження

- `FIREFLY_URL` (внутрішнє ім'я+порт контейнера Firefly) та ім'я Docker-мережі — взяти з наявного стека Firefly на Pi.
- Чи включати в код вбудовану MCC-таблицю одразу (вимкнену) чи додати пізніше.
