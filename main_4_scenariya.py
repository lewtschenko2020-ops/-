"""
Имитация смарт-контракта страховой выплаты (без блокчейна).
Реальный кейс: засуха в Русско-Полянском районе Омской области, 2026.

Правила:
  1) Засуха = страховой случай, если введён режим ЧС.
  2) Страхователь уведомляет страховую в течение 24 часов.

Условия:
  U1 — факт засухи (режим ЧС + спутниковое подтверждение)
  U2 — акт агрономического обследования поля (% гибели растений)
  U3 — метеоданные по осадкам / влажности почвы в точке поля
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smart_contract")


# ---------------------------------------------------------------------------
# Состояния конечного автомата
# ---------------------------------------------------------------------------
class State(str, Enum):
    POLICY_ACTIVE   = "POLICY_ACTIVE"
    EVENT_REPORTED  = "EVENT_REPORTED"
    CHECKING        = "CHECKING"
    ADVANCE_PAID    = "ADVANCE_PAID"
    REJECTED        = "REJECTED"
    WAITING_DATA    = "WAITING_DATA"


TRANSITIONS: Dict[State, Set[State]] = {
    State.POLICY_ACTIVE:  {State.EVENT_REPORTED, State.WAITING_DATA, State.REJECTED},
    State.EVENT_REPORTED: {State.CHECKING, State.WAITING_DATA, State.REJECTED},
    State.CHECKING:       {State.ADVANCE_PAID, State.REJECTED, State.WAITING_DATA},
    State.WAITING_DATA:   {State.CHECKING, State.REJECTED, State.WAITING_DATA},
    State.ADVANCE_PAID:   set(),
    State.REJECTED:       set(),
}


@dataclass
class StateMachine:
    state: State = State.POLICY_ACTIVE
    history: List[Tuple[State, State, str]] = field(default_factory=list)

    def can_transition(self, to_state: State) -> bool:
        return to_state in TRANSITIONS[self.state]

    def transition(self, to_state: State, reason: str = "") -> State:
        if not self.can_transition(to_state):
            log.error("Недопустимый переход %s -> %s (%s)",
                      self.state.value, to_state.value, reason)
            raise ValueError(f"Переход {self.state.value} -> {to_state.value} запрещён")
        log.info("Переход состояния: %s -> %s | причина: %s",
                 self.state.value, to_state.value, reason or "—")
        self.history.append((self.state, to_state, reason))
        self.state = to_state
        return self.state


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return " ".join(str(s).lower().replace("-", " ").split())


def _territory_matches(farm_district: str, act_territory: str) -> bool:
    a, b = _norm(farm_district), _norm(act_territory)
    if not a or not b:
        return False
    return a in b or b in a


def _is_emergency_act(event: Dict[str, Any]) -> bool:
    etype = _norm(event.get("type"))
    org   = _norm(event.get("organization"))
    title = _norm(event.get("title") or event.get("описание") or "")
    return (
        "emergency" in etype
        or "чс" in etype
        or "мчс" in org
        or "чс" in title
        or "указ" in title
    )


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------
REQUIRED_CONDITIONS = ("U1", "U2", "U3")

CONDITION_MEANING = {
    "U1": "Факт засухи (режим ЧС + спутниковое подтверждение)",
    "U2": "Акт агрономического обследования поля (% гибели растений)",
    "U3": "Метеоданные по осадкам / влажности почвы в точке поля",
}


def process(
    contract: Dict[str, Any],
    events: List[Dict[str, Any]],
    notification_datetime: Optional[datetime] = None,
) -> Dict[str, Any]:
    sm = StateMachine(state=State.POLICY_ACTIVE)
    sm.transition(State.EVENT_REPORTED, "Получены данные о событии")

    # --- параметры контракта ---
    start = _parse_date(contract.get("start_date"))
    end   = _parse_date(contract.get("end_date"))
    farm_district = contract.get("район") or contract.get("district")
    insurance_sum = float(contract.get("страховка") or contract.get("insurance") or 0)
    advance_share = float(contract.get("доля_аванса") or contract.get("advance_share") or 0.30)

    if start is None or end is None:
        sm.transition(State.WAITING_DATA, "Не указан период страхования")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": ["start_date/end_date в contract"],
            "reasons": ["Не указан период действия полиса"],
            "state": sm.state.value,
        }

    if not farm_district:
        sm.transition(State.WAITING_DATA, "Не указан район хозяйства")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": ["район в contract"],
            "reasons": ["Не указан район хозяйства"],
            "state": sm.state.value,
        }

    sm.transition(State.CHECKING, "Начинаем проверку условий U1/U2/U3")

    # --- правило 24 часа ---
    if notification_datetime is not None:
        event_dt = _parse_date(contract.get("event_date")) or start
        deadline = datetime.combine(event_dt, datetime.min.time()) + timedelta(hours=24)
        if notification_datetime > deadline:
            log.warning("Уведомление позже 24 часов: %s > %s",
                        notification_datetime, deadline)
            sm.transition(State.REJECTED, "Нарушено правило уведомления 24 часа")
            return {
                "status": "NO_PAY",
                "reasons": [
                    f"Уведомление подано позже 24 часов "
                    f"({notification_datetime} > {deadline})"
                ],
                "state": sm.state.value,
            }
        log.info("Правило 24 часов соблюдено: %s <= %s",
                 notification_datetime, deadline)

    # --- фильтруем verified в периоде ---
    verified_events: List[Dict[str, Any]] = []
    for ev in events or []:
        if str(ev.get("статус", ev.get("status", ""))).lower() != "verified":
            continue
        ev_date = _parse_date(ev.get("дата") or ev.get("date"))
        if ev_date is None:
            continue
        if not (start <= ev_date <= end):
            log.info("Подтверждение %s (%s) вне периода страхования (%s..%s) — игнорируется",
                     ev.get("id"), ev_date, start, end)
            continue
        verified_events.append({**ev, "_date": ev_date})

    # --- раскладываем по условиям ---
    covered: Dict[str, List[Dict[str, Any]]] = {c: [] for c in REQUIRED_CONDITIONS}
    for ev in verified_events:
        cond = str(ev.get("условие") or ev.get("condition") or "").upper()
        if cond in covered:
            covered[cond].append(ev)

    # --- правило: засуха = страховой случай только при ЧС ---
    has_emergency = any(_is_emergency_act(ev) for ev in verified_events)
    if not has_emergency:
        sm.transition(State.WAITING_DATA, "Нет акта ЧС")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": [
                "U1: отсутствует акт о введении режима ЧС "
                "(правило: засуха = страховой случай только при ЧС)"
            ],
            "reasons": ["Засуха не признаётся страховым случаем без введённого режима ЧС"],
            "state": sm.state.value,
        }

    # --- проверяем закрытие U1/U2/U3 ---
    missing: List[str] = []
    for cond in REQUIRED_CONDITIONS:
        if not covered[cond]:
            missing.append(f"{cond}: {CONDITION_MEANING[cond]}")
        else:
            for ev in covered[cond]:
                log.info("Условие %s закрыто подтверждением id=%s от '%s' (тип: %s, дата: %s)",
                         cond, ev.get("id"), ev.get("organization"),
                         ev.get("type"), ev.get("_date"))

    if missing:
        sm.transition(State.WAITING_DATA, "Не все условия подтверждены")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": missing,
            "reasons": [
                "Открытых данных достаточно для фиксации факта засухи, "
                "но недостаточно для однозначного вывода о страховом случае и размере ущерба"
            ],
            "state": sm.state.value,
        }

    # --- проверка территории ---
    territory_ok = False
    territory_checked = 0
    for cond in REQUIRED_CONDITIONS:
        for ev in covered[cond]:
            act_territory = ev.get("территория") or ev.get("territory") or ""
            territory_checked += 1
            if _territory_matches(farm_district, act_territory):
                territory_ok = True
                log.info("Территория подтверждена по условию %s: '%s' ⊆ '%s'",
                         cond, farm_district, act_territory)
                break
        if territory_ok:
            break

    if territory_checked == 0:
        sm.transition(State.WAITING_DATA, "Нет данных о территории")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": ["территория в подтверждениях"],
            "reasons": ["Не указана территория действия акта"],
            "state": sm.state.value,
        }

    if not territory_ok:
        sm.transition(State.REJECTED, "Район хозяйства не входит в территорию акта")
        return {
            "status": "NO_PAY",
            "reasons": [
                f"Район '{farm_district}' не входит в территорию действия акта"
            ],
            "state": sm.state.value,
        }

    # --- независимые источники ---
    all_used = [ev for cond in REQUIRED_CONDITIONS for ev in covered[cond]]
    pairs = {(_norm(ev.get("organization")), _norm(ev.get("type"))) for ev in all_used}
    log.info("Независимых пар организация+тип: %d", len(pairs))

    if len(pairs) < 2:
        sm.transition(State.WAITING_DATA, "Меньше 2 независимых источников")
        return {
            "status": "NOT_ENOUGH_DATA",
            "missing": [
                "Требуется минимум 2 независимых источника (разные организации и типы)"
            ],
            "reasons": [f"Независимых пар организация+тип: {len(pairs)} (<2)"],
            "independent_sources": len(pairs),
            "state": sm.state.value,
        }

    # --- PAY ---
    payout = round(insurance_sum * advance_share, 2)
    sm.transition(State.ADVANCE_PAID, "Все условия подтверждены")
    log.info("Сработало правило: U1+U2+U3 подтверждены, территория совпадает, "
             "≥2 независимых источника. Аванс = %.2f (%.2f × %.2f)",
             payout, insurance_sum, advance_share)

    return {
        "status": "PAY",
        "amount": payout,
        "reasons": ["Все условия U1/U2/U3 подтверждены независимыми источниками"],
        "independent_sources": len(pairs),
        "state": sm.state.value,
    }


# ---------------------------------------------------------------------------
# Тестовые сценарии
# ---------------------------------------------------------------------------
def _base_contract(**overrides: Any) -> Dict[str, Any]:
    """Базовый контракт: Омская обл., Русско-Полянский р-н, 2026."""
    c = {
        "регион": "Омская область",
        "район": "Русско-Полянский",
        "культура": "зерновые",
        "площадь": 1500,
        "страховка": 5_000_000,
        "start_date": "2026-04-01",
        "end_date":   "2026-10-31",
        "доля_аванса": 0.30,
        "event_date": "2026-08-04",
    }
    c.update(overrides)
    return c


def _full_event_set(territory: str = "Русско-Полянский район") -> List[Dict[str, Any]]:
    """Полный набор verified-подтверждений U1, U2, U3."""
    return [
        # U1: акт ЧС
        {
            "id": "ACT-108-r",
            "условие": "U1",
            "organization": "МЧС России",
            "type": "emergency_act",
            "title": "Указ №108-р о введении режима ЧС",
            "url": "https://55.mchs.gov.ru/...",
            "date": "2026-08-04",
            "territory": territory,
            "статус": "verified",
        },
        # U1: спутник
        {
            "id": "COPERNICUS-2026-08-11",
            "условие": "U1",
            "organization": "Copernicus / ESA",
            "type": "satellite",
            "title": "Снимок 11.08.26: сильная засуха",
            "url": "https://browser.dataspace.copernicus.eu/",
            "date": "2026-08-11",
            "territory": territory,
            "статус": "verified",
        },
        # U2: акт агрономического обследования
        {
            "id": "AGRO-2026-08-15",
            "условие": "U2",
            "organization": "Независимый оценщик",
            "type": "agro_expert",
            "title": "Акт агрообследования: гибель 45% растений",
            "url": "https://expert.example/agro-2026-08-15",
            "date": "2026-08-15",
            "territory": territory,
            "статус": "verified",
        },
        # U3: метеоданные
        {
            "id": "METEO-2026-08-10",
            "условие": "U3",
            "organization": "Росгидромет",
            "type": "meteo",
            "title": "Осадки ниже нормы в 5 раз, влажность почвы 12%",
            "url": "https://meteo.example/2026-08-10",
            "date": "2026-08-10",
            "territory": territory,
            "статус": "verified",
        },
    ]


def _print_result(name: str, expected: str, result: Dict[str, Any]) -> None:
    actual = result.get("status")
    ok = "✅" if actual == expected else "❌"
    print("\n" + "=" * 70)
    print(f"СЦЕНАРИЙ: {name}")
    print(f"Ожидалось: {expected} | Получено: {actual}  {ok}")
    print("-" * 70)
    for k, v in result.items():
        if k == "state":
            continue
        print(f"  {k}: {v}")
    print(f"  state: {result.get('state')}")
    print("=" * 70)


def main() -> None:
    """Запускает 4 тестовых сценария и выводит результаты."""

    # ---------- Сценарий 1: PAY ----------
    print("\n>>> Сценарий 1: полис действует, акт ЧС есть, снимки показывают ущерб")
    contract_1 = _base_contract()
    events_1 = _full_event_set()
    result_1 = process(contract_1, events_1,
                       notification_datetime=datetime(2026, 8, 4, 18, 30))
    _print_result(
        "Полис действует, ЧС, снимки с ущербом",
        "PAY",
        result_1,
    )

    # ---------- Сценарий 2: NO_PAY (полис закончился 30.06.2026) ----------
    print("\n>>> Сценарий 2: полис закончился 30.06.2026, событие в августе")
    contract_2 = _base_contract(end_date="2026-06-30")
    events_2 = _full_event_set()
    result_2 = process(contract_2, events_2,
                       notification_datetime=datetime(2026, 8, 4, 18, 30))
    _print_result(
        "Полис истёк 30.06.2026, событие в августе",
        "NO_PAY",
        result_2,
    )

    # ---------- Сценарий 3: NO_PAY (район не в перечне территорий акта) ----------
    print("\n>>> Сценарий 3: район не в перечне территорий акта")
    contract_3 = _base_contract(район="Омский")   # другой район
    events_3 = _full_event_set(territory="Русско-Полянский район")
    result_3 = process(contract_3, events_3,
                       notification_datetime=datetime(2026, 8, 4, 18, 30))
    _print_result(
        "Район хозяйства не входит в территорию акта",
        "NO_PAY",
        result_3,
    )

    # ---------- Сценарий 4: NOT_ENOUGH_DATA ----------
    # По повреждению (U2) только одно подтверждение, и оно unverified.
    # Значит U2 не закрыто → NOT_ENOUGH_DATA.
    print("\n>>> Сценарий 4: U2 подтверждено одним unverified-источником")
    contract_4 = _base_contract()
    events_4 = _full_event_set()
    # Заменяем U2 на unverified
    events_4 = [
        ev for ev in events_4 if ev["условие"] != "U2"
    ] + [
        {
            "id": "AGRO-UNVERIFIED-1",
            "условие": "U2",
            "organization": "Фермер Иванов",
            "type": "self_report",
            "title": "Сам сообщил о гибели растений, без акта",
            "url": "https://example.com/self",
            "date": "2026-08-15",
            "territory": "Русско-Полянский район",
            "статус": "unverified",     # <-- не подтверждено
        }
    ]
    result_4 = process(contract_4, events_4,
                       notification_datetime=datetime(2026, 8, 4, 18, 30))
    _print_result(
        "U2 только одно и оно unverified",
        "NOT_ENOUGH_DATA",
        result_4,
    )

    # ---------- Итоговая сводка ----------
    print("\n\n" + "#" * 70)
    print("# СВОДКА ПО СЦЕНАРИЯМ")
    print("#" * 70)
    summary = [
        ("1. Полис действует, ЧС, снимки с ущербом", "PAY",            result_1),
        ("2. Полис истёк 30.06.2026",                "NO_PAY",         result_2),
        ("3. Район не в перечне акта",               "NO_PAY",         result_3),
        ("4. U2 только одно и unverified",           "NOT_ENOUGH_DATA", result_4),
    ]
    for name, expected, res in summary:
        actual = res.get("status")
        mark = "✅" if actual == expected else "❌"
        print(f"  {mark} {name:<45} ожидалось: {expected:<16} получено: {actual}")


if __name__ == "__main__":
    main()