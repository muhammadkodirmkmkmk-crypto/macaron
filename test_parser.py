"""Проверка разбора текста на реальных примерах из группы. python test_parser.py"""
from app import parser as P

CASES = [
    # (текст, продукт, минуты, качество)
    ("Pero 10 soat 30minutda chiqdi",              "Pero", 630, "unknown"),
    ("Burama 9 soat 30 minutda chiqdi",            "Burama", 570, "unknown"),
    ("Burama 8 soat 50 minutda chiqdi",            "Burama", 530, "unknown"),
    ("Pautinka 14 soatda chiqdi trewena yuq",      "Pautinka", 840, "ok"),
    ("Burama 9 soat 10 minutda chiqdi",            "Burama", 550, "unknown"),
    ("spral 11 soat 20 daqiqada chiqdi trewena bor", "Spiral", 680, "defect"),
    ("Rojok 12 soatda chiqdi",                     "Rojok", 720, "unknown"),
    ("sushka 27 pero 10 soat chiqdi",              "Pero", 600, "unknown"),
    ("Вермишель 7 часов 40 минут готово",          "Vermishel", 460, "unknown"),
    ("Pautunka 13 soat 05 minutda chiqdi",         "Pautinka", 785, "unknown"),
    ("Zirak 11 soat 40 minutda chiqdi",            "Zirak", 700, "unknown"),
]

NOT_REPORTS = ["XoN", "zor zor 😂", "salom", "https://uzfiltr.uz/"]


# --- часы захода/выхода ---
CLOCK_CASES = [
    ("00:28 kirgan 10:30 chiqdi burama",        (0, 28), (10, 30), 602, "Burama"),
    ("23:40 kirgan 09:15 chiqdi pero",          (23, 40), (9, 15), 575, "Pero"),
    ("kirgan 01:05 chiqqan 11:35 spiral",       (1, 5), (11, 35), 630, "Spiral"),
    ("Burama 22:00 kirdi 08:30 chiqdi",         (22, 0), (8, 30), 630, "Burama"),
    ("зашла в 03:20 вышла в 13:50 вермишель",   (3, 20), (13, 50), 630, "Vermishel"),
    ("00.28 kirgan 10.30 chiqdi",               (0, 28), (10, 30), 602, None),
    ("14:27 yopildi 23:50 ochildi burama",      (14, 27), (23, 50), 563, "Burama"),
]


def check_clock() -> int:
    bad = 0
    for text, st, fin, mins, prod in CLOCK_CASES:
        p = P.parse_text(text)
        ok = (p.started_hm == st and p.finished_hm == fin
              and p.duration_minutes == mins and p.product == prod)
        bad += not ok
        print(f"{'✓' if ok else '✗'} {text[:40]:42} -> {p.started_hm}→{p.finished_hm} "
              f"{p.duration_minutes} мин / {p.product}")
    return bad


LOAD_CASES = [
    # «yopildi» = закрыли дверь сушки = начало сушки, так пишут в цехе
    ("14:27 burama yopildi",            (14, 27)),
    ("14:40 yopildi zirak",             (14, 40)),
    ("13:27 zirak yopildi",             (13, 27)),
    ("00:28 kirdi burama",              (0, 28)),
    ("12-sushka 01:05 kirgan pero",     (1, 5)),
    ("burama kirdi",                    (-1, -1)),   # времени нет — считаем от сообщения
    ("заложили в 03:20 вермишель",      (3, 20)),
    ("00:28 kirgan 10:30 chiqdi burama", None),      # есть выход — это готовая партия
    ("Burama 9 soat 30 minutda chiqdi", None),
    ("10:30 chiqdi",                    None),
    ("14:27 yopildi 23:50 ochildi",     None),   # есть и открытие — готовая партия
    ("zor zor",                         None),
]


def check_load() -> int:
    bad = 0
    for text, exp in LOAD_CASES:
        got = P.parse_load_only(text)
        ok = got == exp
        bad += not ok
        print(f"{'✓' if ok else '✗'} загрузка: {text[:38]:40} -> {got}")
    return bad


STOP_CASES = [
    # новый формат цеха: старт и стоп — разными сообщениями
    ("Stop vaqt 22:46 burama",          (22, 46)),
    ("Stop vaqt 23:34 zirak",           (23, 34)),
    ("12 Stop vaqt 08:15 qochqor",      (8, 15)),
    ("Start vaqt 23:26 qochqor",        None),   # это заход, не выход
    ("Burama 9 soat 30 minutda chiqdi", None),   # длительность написана прямо
    ("00:28 kirgan 10:30 chiqdi burama", None),  # заход и выход в одном сообщении
    ("zor zor",                         None),
]

START_CASES = [
    ("Start vaqt 23:26 qochqor",  (23, 26), "Qochqor", None),
    ("Start vaqt 00:05 burama",   (0, 5),   "Burama",  None),
    ("7 Start vaqt 14:10 pero",   (14, 10), "Pero",    7),
    ("Stop vaqt 22:46 burama",    None,     "Burama",  None),
]


def check_startstop() -> int:
    bad = 0
    for text, exp in STOP_CASES:
        got = P.parse_stop_only(text)
        ok = got == exp
        bad += not ok
        print(f"{'✓' if ok else '✗'} выгрузка: {text[:38]:40} -> {got}")
    print()
    for text, exp_hm, exp_prod, exp_dryer in START_CASES:
        hm = P.parse_load_only(text)
        p = P.parse_text(text)
        ok = hm == exp_hm and p.product == exp_prod and p.dryer_number == exp_dryer
        bad += not ok
        print(f"{'✓' if ok else '✗'} заход:    {text[:38]:40} -> {hm} / {p.product} / №{p.dryer_number}")
    return bad


def check_vision() -> int:
    """С фото берём только номер сушки и показания табло — время работы НЕ берём."""
    bad = 0
    out = P._merge(P.parse_text(""), {
        "dryer_number": 16, "dryer_number_confidence": 0.9,
        "display_line1": "0959",   # это ОСТАВШЕЕСЯ время программы
        "display_line2": "45.8", "display_line3": "46",
        "hours": 9, "minutes": 59,  # даже если модель это прислала — игнорируем
    })
    for label, cond in (
        ("время работы с фото не берём", out.duration_minutes is None),
        ("номер сушки с фото берём", out.dryer_number == 16),
        ("температура 45.8", out.temperature == 45.8),
        ("влажность 46", out.humidity == 46),
        ("остаток программы в timer_raw", out.timer_raw == "09:59"),
        ("голое фото — не отчёт", P.is_report_message("", has_photo=True) is False),
        ("фото с подписью — отчёт", P.is_report_message("Pero 10 soat chiqdi", True) is True),
    ):
        bad += not cond
        print(f"{'✓' if cond else '✗'} {label}")
    return bad


def check_boilers() -> int:
    from app import config
    expect = {1: 1, 12: 1, 13: 2, 22: 2, 23: 3, 31: 3, 32: None, None: None}
    bad = 0
    for n, b in expect.items():
        got = config.boiler_of(n)
        ok = got == b
        bad += not ok
        print(f"{'✓' if ok else '✗'} котёл: сушка {n} -> {got}")
    return bad


def main() -> int:
    bad = 0
    for text, prod, mins, qual in CASES:
        p = P.parse_text(text)
        ok = (p.product == prod and p.duration_minutes == mins and p.quality == qual)
        bad += not ok
        print(f"{'✓' if ok else '✗'} {text[:45]:47} -> {p.product} / {p.duration_minutes} / {p.quality}")

    for text in NOT_REPORTS:
        ok = not P.is_report_message(text, has_photo=False)
        bad += not ok
        print(f"{'✓' if ok else '✗'} не отчёт: {text!r}")

    n27 = P.parse_text("sushka 27 pero 10 soat chiqdi")
    assert n27.dryer_number == 27, "номер сушки из текста"

    print()
    bad += check_clock()

    print()
    bad += check_load()

    print()
    bad += check_startstop()

    print()
    bad += check_vision()

    print()
    bad += check_boilers()

    print(f"\n{'ВСЁ ОК' if not bad else f'ОШИБОК: {bad}'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
