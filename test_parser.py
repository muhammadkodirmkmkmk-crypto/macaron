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

    print(f"\n{'ВСЁ ОК' if not bad else f'ОШИБОК: {bad}'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
