# 1. Цель

При проведении бэктеста Claude должен:

1. сохранять каждый протестированный торговый кейс в основной таблице `Backtest Database`;
2. классифицировать кейсы по направлению, модели, сессии, дню недели и месяцу;
3. рассчитывать основные показатели стратегии;
4. формировать агрегированные таблицы;
5. на их основе формировать основной Backtest Report.

---

# 2. Основная таблица: `Backtest Database`

Одна строка = один протестированный торговый кейс.

| Поле | Содержание |
|---|---|
| `Case #` | ID кейса |
| `Pair` | Инструмент, например XAUUSD |
| `Date` | Дата и время сделки |
| `Weekday` | День недели |
| `Direction` | Long / Short |
| `Models` | Торговая модель |
| `Sessions` | Торговая сессия |
| `Months` | Месяц тестирования |
| `RR` | Потенциальный Risk/Reward сделки |
| `Result` | Win / Lose / BE |
| `Risk` | Риск сделки в R |
| `Result Pct` | Фактически полученный результат в R |
| `Win?` | Yes / No |
| `Lose?` | Yes / No |
| `BE?` | Yes / No |
| `BE Reason` | Причина перевода/закрытия в BE |
| `News Event` | Значимое экономическое событие |
| `Mistake` | Ошибка, замеченная при разборе сделки |
| `To Improve` | Вывод или замечание для улучшения стратегии |
| `Needs validation` | Требует ли кейс дополнительной проверки |
| `Past Month?` | Служебный признак месяца |

### Значения классификаторов из текущего примера

**Direction**
- Long
- Short

**Models**
- BOS
- Inversion
- Engulfing

**Sessions**
- Asia
- Frankfurt
- London
- Lunch
- New York

**Result**
- Win
- Lose
- BE

Для прибыльной сделки:

`Result Pct = RR`

Для Loss:

`Result Pct = -Risk`

Для BE:

`Result Pct = 0`

---

# 3. Агрегированные таблицы

Все таблицы ниже формируются на основании `Backtest Database`.

## 3.1. `Total Statistics`

Содержит четыре основных показателя.

| Metric | Расчёт |
|---|---|
| `Total Trades` | количество всех сделок |
| `Winrate` | Wins / (Wins + Losses) |
| `Gained RR` | сумма `Result Pct` |
| `Average RR` | среднее значение `RR` |

Для текущего XAUUSD-бэктеста формат результата выглядит так:

| Metric | Value |
|---|---:|
| Total Trades | 184 |
| Winrate | 78.95% |
| Gained RR | 286.11 RR |
| Average RR | 2.72 RR |

---

## 3.2. `Direction`

Одна строка на каждое направление.

| Field | Description |
|---|---|
| Name | Long / Short |
| Total Trades | количество сделок |
| Winrate | winrate направления |

Пример структуры:

| Direction | Trades | Winrate |
|---|---:|---:|
| Long | 148 | 80% |
| Short | 36 | 77% |

---

## 3.3. `Models`

Одна строка на каждую торговую модель.

| Field | Description |
|---|---|
| Name | название модели |
| Total Trades | количество сделок |
| Winrate | winrate |
| Average RR | средний RR |

Используемые в примере модели:

| Model | Trades | Winrate | Average RR |
|---|---:|---:|---:|
| BOS | 88 | 73.5% | 2.76 |
| Inversion | 85 | 83.8% | 2.72 |
| Engulfing | 11 | 80% | 2.42 |

---

## 3.4. `Sessions`

Одна строка на каждую торговую сессию.

| Field | Description |
|---|---|
| Name | название сессии |
| Time | временной диапазон |
| Total Trades | количество сделок |
| Winrate by Session | winrate |

Структура текущего примера:

| Session | Time | Trades | Winrate |
|---|---|---:|---:|
| Asia | 03:00-06:00 | 16 | 53% |
| Frankfurt | 09:00-10:00 | 4 | 100% |
| London | 10:00-12:00 | 45 | 79% |
| Lunch | 12:00-14:00 | 31 | 80% |
| New York | 14:00-18:00 | 88 | 84% |

---

## 3.5. `Weekdays`

Одна строка на каждый день недели.

| Field | Description |
|---|---|
| Name | Monday-Friday |
| Total Trades | количество сделок |
| Winrate by Day | winrate |

Формат:

| Day | Trades | Winrate |
|---|---:|---:|
| Monday | 35 | 73% |
| Tuesday | 40 | 82% |
| Wednesday | 42 | 71% |
| Thursday | 34 | 85% |
| Friday | 32 | 86% |

---

## 3.6. `Months`

Одна строка = один календарный месяц.

| Field | Description |
|---|---|
| Month | месяц и год |
| Positions | количество сделок |
| PnL | суммарный результат месяца |

Пример:

| Month | Positions | PnL |
|---|---:|---:|
| 2025 Jun | 8 | 15% |
| 2025 Jul | 8 | 20.2% |
| 2025 Aug | 8 | 12% |
| 2025 Sep | 9 | 17.75% |
| 2025 Oct | 12 | 25% |

---

# 4. Расчёт статистики

Для каждой сделки Claude определяет:

```text
IF Result = Win:
    Win = 1
    Loss = 0
    BE = 0
    Result = RR

IF Result = Lose:
    Win = 0
    Loss = 1
    BE = 0
    Result = -Risk

IF Result = BE:
    Win = 0
    Loss = 0
    BE = 1
    Result = 0
```

После добавления всех сделок рассчитываются:

```text
Total Trades = count(all trades)

Winning Trades = count(Result = Win)

Losing Trades = count(Result = Lose)

BE Trades = count(Result = BE)

Winrate =
    Winning Trades /
    (Winning Trades + Losing Trades)

Average RR =
    average(RR)

Gained RR =
    sum(Result Pct)
```

Те же расчёты повторяются отдельно для каждой категории:

```text
Direction
Model
Session
Weekday
Month
```

---

# 5. Основной Backtest Report

Claude должен формировать один основной отчёт следующего формата.

## Backtest Summary

**Instrument:** `[Pair]`  
**Tested period:** `[first date] - [last date]`

### Total Statistics

| Metric | Value |
|---|---:|
| Total Trades | |
| Wins | |
| Losses | |
| BE | |
| Winrate | |
| Average RR | |
| Gained RR | |

### Performance by Direction

| Direction | Trades | Winrate |
|---|---:|---:|
| Long | | |
| Short | | |

### Performance by Model

| Model | Trades | Winrate | Average RR |
|---|---:|---:|---:|
| BOS | | | |
| Inversion | | | |
| Engulfing | | | |

### Performance by Session

| Session | Time | Trades | Winrate |
|---|---|---:|---:|
| Asia | | | |
| Frankfurt | | | |
| London | | | |
| Lunch | | | |
| New York | | | |

### Performance by Weekday

| Weekday | Trades | Winrate |
|---|---:|---:|
| Monday | | |
| Tuesday | | |
| Wednesday | | |
| Thursday | | |
| Friday | | |

### Monthly Performance

| Month | Trades | PnL |
|---|---:|---:|
| YYYY-MM | | |

После таблиц Claude выводит списки сделок, в которых заполнены:

- `BE Reason`
- `News Event`
- `Mistake`
- `To Improve`
- `Needs validation`

Все цифры основного отчёта должны рассчитываться из `Backtest Database`. Агрегированные таблицы `Total Statistics`, `Direction`, `Models`, `Sessions`, `Weekdays` и `Months` являются представлениями одной основной базы данных.