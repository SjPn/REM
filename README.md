# EstateMonitor

Мониторинг рынка **коммерческой недвижимости Киева и Киевской области**: объявления продажи/аренды, появление новых объектов, исчезновение и гипотезы сделок.

## Порталы (MVP)

| Источник | Зачем | URL-якоря |
|----------|--------|-----------|
| **LUN** | Лучшее покрытие Киева, дедуп «по помещениям» | `/sale|rent/kyiv/commercial` |
| **OLX** | Максимальный объём, собственники + посредники | commercial Kyiv sale/rent |
| **DOM.RIA** | Крупный нац. портал, коммерческий сегмент | search commercial |
| **RIELTOR.UA** | Сильный блок офисов/коммерции | `/kyiv/commerce-*`, offices |

Позже (не в MVP): 100realty, Address.ua, узкие office-каталоги.

## Как отличить сделку от снятия

Система **не ставит факт «продано»**, а считает `DealHypothesis`:

- score 0–100
- bucket: `likely_deal` / `ambiguous` / `likely_withdrawn`
- фичи: исчезло со всех источников, явный статус, падение цены, DOM, bulk-delist агентства, перепубликация и т.д.

Ручная разметка на карточке объекта (`deal` / `withdrawn`) нужна для калибровки порогов.

## Стек

- Python 3.11+
- FastAPI + Jinja UI
- SQLAlchemy + SQLite (по умолчанию в `data/estatemonitor.db`)
- httpx + BeautifulSoup парсеры

## Быстрый старт

```powershell
cd d:\MyPyPro\estatemonitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env

python -m scripts.cli init
python -m scripts.cli demo
python -m scripts.cli serve
```

Открыть: http://127.0.0.1:8000

### Crawl живых порталов

```powershell
# 1) Первичное наполнение (много страниц, цены с выдачи, без vanish)
python -m scripts.cli backfill

# 2) Углубить телефоны/статусы (медленнее)
python -m scripts.cli backfill --with-details

# 3) Ежедневный режим
python -m scripts.cli scheduler --run-now
# или Windows Task:
.\scripts\install_windows_task.bat

python -m scripts.cli stats
```

**Логика после наполнения:** каждый день crawler снова обходит порталы → новые объекты, изменения цены, исчезновения → `DealHypothesis`. Vanish включается только если за проход увидели достаточно объявлений (`MIN_SEEN_FOR_VANISH`), чтобы частичный сбой не обнулил базу.

> Живые сайты часто меняют вёрстку / режут ботов. OLX часто 403 с датацентровых IP.

## API (основное)

- `GET /api/stats`
- `GET /api/listings?status=active&source=lun`
- `GET /api/properties`
- `GET /api/properties/{id}`
- `GET /api/deals?bucket=likely_deal`
- `POST /api/deals/{id}/label` `{"human_label":"deal|withdrawn|unknown"}`
- `POST /api/crawl?max_pages=2&sources=lun,olx`
- `POST /api/demo/seed`
- `GET /api/events`
- `GET /api/crawls`

## Архитектура данных

`Property` (канонический объект по fingerprint) ← `Listing` (объявление на источнике) ← `ListingSnapshot`  
События: `PropertyEvent` (`appeared`, `price_changed`, `vanished`, `relisted`, …)  
Гипотезы: `DealHypothesis`

Fingerprint: нормализованный адрес + площадь + этаж + тип + deal_type (+ coords если есть).

## Сегмент мониторинга

В базе **только целевой продукт**:

- офисы
- шоурумы
- торговые / street retail / первые этажи
- бизнес-центры
- отдельные коммерческие здания
- помещения свободного назначения (если не склад/производство)

**Исключаем:** склады, логистика, производство, промкомплексы, земля, гаражи.

Фильтр: `app/domain/segments.py` (на ingest). Чистка уже собранного: `python -m scripts.cli prune-irrelevant`.

Полного 100% рынка Киев+область система не гарантирует: покрытие = то, что отдают LUN / DOM.RIA / RIELTOR (+ OLX если доступен), минус антибот/вёрстка/пагинация.

## Detail enrichment

После list-crawl система открывает карточки объявлений и добирает:

- точный адрес / район / координаты
- телефон
- агентство / риелтор
- явный статус (`sold` / `rented` / `inactive_404` / `active`)

Управление через `.env`:

```
ENRICH_DETAILS=true
MAX_DETAIL_PAGES=40
HTTP_VERIFY_SSL=false
```

Парсеры:

- **LUN** — JSON-LD ItemList на выдаче + `/realty/{id}` detail
- **DOM.RIA** — list + Product/LocalBusiness JSON-LD на карточке
- **RIELTOR** — `/commercials-*/view/{id}/` + телефоны/адрес
- **OLX** — list + detail (часто 403 с datacenter IP)
