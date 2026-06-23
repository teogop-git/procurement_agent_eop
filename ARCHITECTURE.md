# Procurement Agent – Архитектура

**Версия:** IMAP + Local LLM с Cloud Fallback  
**Клиент:** Paraflow AI | paraflow.ai@paraflow.bg  
**Дата:** Април 2026

---

## Обща идея

Агентът се събужда на всеки 4 часа, проверява входящата поща за имейли от ЦАИС ЕОП,
сваля техническата документация на новите поръчки, изпраща я към AI модел за анализ
и записва структуриран Word репорт с намерените vendor-lock индикатори.

---

## Поток на данните

```
┌─────────────────────────┐
│  ЦАИС ЕОП               │
│  noreply@eop.bg         │  ← изпраща имейл при нова поръчка
└────────────┬────────────┘
             │ имейл
             ▼
┌─────────────────────────┐
│  Email Monitor          │  ← IMAP SSL към mail.paraflow.bg:993
│  (email_monitor.py)     │    username + password, без OAuth
│                         │    извлича URLs към app.eop.bg
└────────────┬────────────┘
             │ списък с URLs
             ▼
┌─────────────────────────┐
│  Document Processor     │  ← scrape страницата на поръчката
│  (document_processor.py)│    свали PDF / DOCX / ZIP
│                         │    извлечи текст (PyPDF2, python-docx)
└────────────┬────────────┘
             │ суров текст
             ▼
┌─────────────────────────────────────────────────────────┐
│  Vendor-Lock Analyzer  (vendor_analyzer.py)             │
│                                                         │
│   1. Опит: Local LLM (Ollama)                          │
│      └─ qwen2.5:14b-instruct  (препоръчан)             │
│         llama3.1:70b          (по-бавен, по-точен)     │
│                                                         │
│   2. Fallback: Cloud LLM (ако local е недостъпен)      │
│      ├─ Anthropic Claude (claude-sonnet-4-20250514)    │
│      └─ OpenAI GPT-4o    (ако Anthropic също е надолу) │
│                                                         │
│   Връща структуриран JSON с:                           │
│   • hardware_specifications  (vendor-specific части)   │
│   • software_requirements    (proprietary изисквания)  │
│   • certifications           (нестандартни сертификати)│
│   • indirect_indicators      (заобиколени ограничения) │
│   • compliant_reformulations (как да се поправят)      │
└────────────┬────────────────────────────────────────────┘
             │ JSON анализ
             ▼
┌─────────────────────────┐
│  Report Generator       │  ← python-docx
│  (report_generator.py)  │    титулна страница + метаданни
│                         │    executive summary с брой рискове
│                         │    секция за всяка поръчка
│                         │    таблица с препоръки
└────────────┬────────────┘
             │ .docx файл
             ▼
┌─────────────────────────┐
│  Local File Writer      │  ← /app/output/reports/YYYY-MM-DD/
│  (file_writer.py)       │    организира по дата
└─────────────────────────┘
```

---

## LLM Fallback верига

```
Заявка за анализ
      │
      ▼
┌─────────────────┐   OK    ┌──────────────────────┐
│  Local Ollama   │────────▶│  JSON резултат        │
│  qwen2.5:14b    │         └──────────────────────┘
└────────┬────────┘
    FAIL │ (timeout / недостъпен / лош JSON)
         ▼
┌─────────────────┐   OK    ┌──────────────────────┐
│  Anthropic      │────────▶│  JSON резултат        │
│  Claude Sonnet  │         └──────────────────────┘
└────────┬────────┘
    FAIL │ (no API key / quota)
         ▼
┌─────────────────┐   OK    ┌──────────────────────┐
│  OpenAI GPT-4o  │────────▶│  JSON резултат        │
└────────┬────────┘         └──────────────────────┘
    FAIL │
         ▼
   Записва грешка в лога,
   пропуска документа
```

Приоритетът се определя от `.env`:

```env
LLM_PRIORITY=local,anthropic,openai   # реда на опит
```

---

## Файлова структура

```
procurement_agent_imap/
│
├── main.py                   Orchestrator – scheduler, координация
│
├── modules/
│   ├── __init__.py
│   ├── email_monitor.py      IMAP клиент
│   ├── document_processor.py Сваляне и извличане на текст
│   ├── vendor_analyzer.py    LLM анализ с fallback верига
│   ├── report_generator.py   Word репорт генератор
│   └── file_writer.py        Запис на файлове
│
├── Dockerfile                Python 3.11-slim образ
├── docker-compose.yml        Конфигурация за deployment
├── requirements.txt          Python зависимости
├── .env.example              Шаблон за конфигурация
├── deploy.sh                 start / stop / logs / test
├── test_config.py            Тест на IMAP + LLM преди старт
└── ARCHITECTURE.md           Този файл
```

---

## Конфигурация (.env)

| Променлива | Описание | Задължителна |
|---|---|---|
| `EMAIL_ADDRESS` | paraflow.ai@paraflow.bg | ✅ |
| `EMAIL_PASSWORD` | Парола за имейл | ✅ |
| `IMAP_SERVER` | mail.paraflow.bg | ✅ |
| `IMAP_PORT` | 993 | ✅ |
| `LOCAL_LLM_ENDPOINT` | http://ollama:11434/api/generate | при local |
| `LOCAL_LLM_MODEL` | qwen2.5:14b-instruct | при local |
| `ANTHROPIC_API_KEY` | sk-ant-... | при fallback |
| `OPENAI_API_KEY` | sk-... | при fallback |
| `LLM_PRIORITY` | local,anthropic,openai | ✅ |
| `CHECK_INTERVAL_HOURS` | 4 | ✅ |
| `RUN_MODE` | scheduled / once | ✅ |

---

## Изисквания за работа

| Компонент | Минимум | Препоръчано |
|---|---|---|
| Docker | 20+ | latest |
| RAM (при local LLM 14B) | 16 GB | 32 GB |
| RAM (при local LLM 70B) | 48 GB | 64 GB |
| Диск | 50 GB | 100 GB |
| Ollama | по желание | latest |

При **само cloud fallback** (без локален модел) – агентът работи на всяка машина с Docker и интернет.

---

## Сигурност

- Имейл паролата живее само в `.env` (chmod 600)
- Документите никога не напускат мрежата при local LLM режим
- При cloud fallback текстът се изпраща към API на трета страна – вземете предвид при чувствителни поръчки
- Няма OAuth, няма Azure AD, няма широки разрешения

---

## Изход

Репортите се записват в:

```
output/
└── reports/
    └── 2026-04-28/
        ├── vendor_lock_report_20260428_081500.docx
        └── vendor_lock_report_20260428_121500.docx
```

Всеки `.docx` файл съдържа:
1. Титулна страница с метаданни
2. Executive Summary (брой HIGH/MEDIUM рискове)
3. По една секция за всяка поръчка:
   - Ниво на риск (цветово кодирано)
   - Хардуерни vendor-lock индикатори
   - Софтуерни изисквания
   - Проблемни сертификации
   - Индиректни индикатори
   - Таблица с препоръчани формулировки
