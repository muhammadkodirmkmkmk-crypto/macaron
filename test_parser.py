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

    print(f"\n{'ВСЁ ОК' if not bad else f'ОШИБОК: {bad}'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
