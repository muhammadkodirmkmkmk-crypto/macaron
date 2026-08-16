# -*- coding: utf-8 -*-
"""Каталог узлов линии и распознавание того, что написали в группе."""

# (ключ, название как говорят на заводе, участок)
PARTS = [
    ("siklon_mator",   "Siklon matori",                      "Un yo'li"),
    ("vinnik_mator",   "Un tortadigan vinnik matori",        "Un yo'li"),
    ("elak_mator",     "Elak matori",                        "Elak"),
    ("elak_chotka",    "Elak chotkasi",                      "Elak"),
    ("elak_setka",     "Elak setkasi",                       "Elak"),
    ("elak_val",       "Elak vali, pachimligi va salniki",   "Elak"),
    ("suvun_pnevmo",   "Suv va un pnevmatikasi",             "Suv va un"),
    ("suvun_chinti",   "Suv va un chinti va relesi",         "Suv va un"),
    ("suvun_rezina",   "Suv va un rezinasi va salniklari",   "Suv va un"),
    ("mish_choynik_b", "Mishalka katta choyniklari sep tomon", "Mishalka"),
    ("mish_sep",       "Mishalka sepi",                      "Mishalka"),
    ("mish_choynik_k", "Mishalka kichkina choynigi",         "Mishalka"),
    ("mish_mator",     "Mishalka matori",                    "Mishalka"),
    ("mish_reduktor",  "Mishalka reduktori",                 "Mishalka"),
    ("shin_mator",     "Shinnik matori",                     "Shinnik"),
    ("shin_reduktor",  "Shinnik reduktori",                  "Shinnik"),
    ("orqa_pachimnik", "Orqa katta pachimnik",               "Shinnik"),
    ("vakum",          "Vakum va matori",                    "Vakum"),
    ("pichoq_mator",   "Pichoq matori",                      "Kesish"),
    ("suv_nasos",      "Suv nasosi",                         "Suv va un"),
    ("lenta_mator",    "Lenta matori",                       "Lenta"),
    ("lenta_talkator", "Lenta talkatori",                    "Lenta"),
    ("lenta_rama",     "Lenta ramasi",                       "Lenta"),
    ("ulitka_mator",   "Ulitka matori",                      "Un yo'li"),
    ("par_katyol",     "Par katyoli",                        "Par"),
    ("untort_remen",   "Un tortgich remeni",                 "Remenlar"),
    ("elak_remen",     "Elak remeni",                        "Remenlar"),
    ("shin_remen",     "Shinnik remeni",                     "Remenlar"),
    ("mish_remen",     "Mishalka remeni",                    "Remenlar"),
    ("vakum_remen",    "Vakum remeni",                       "Remenlar"),
    ("lenta_remen",    "Lenta remeni",                       "Remenlar"),
    ("bunker_reduktor", "Bunker reduktori",                  "Bunker"),
    ("bunker_mator",   "Bunker matori",                      "Bunker"),
    ("bunker_remen",   "Bunker remeni",                      "Remenlar"),
    ("kut_tross",      "Un ko'targich trossi",               "Un ko'targich"),
    ("kut_mator",      "Un ko'targich matori",               "Un ko'targich"),
    ("kut_reduktor",   "Un ko'targich reduktori",            "Un ko'targich"),
    ("kut_remen",      "Un ko'targich remeni",               "Un ko'targich"),
]

NAMES = {k: n for k, n, _ in PARTS}
ZONES = {k: z for k, _, z in PARTS}

# как это пишут в жизни: латиница, кириллица, с ошибками
ALIAS = {
    "mator": "motor", "matori": "motor", "матор": "motor", "мотор": "motor",
    "dvigatel": "motor", "двигатель": "motor",
    "reduktori": "reduktor", "редуктор": "reduktor", "редуктори": "reduktor",
    "remeni": "remen", "ремен": "remen", "ремень": "remen", "remni": "remen",
    "setkasi": "setka", "сетка": "setka", "сеткаси": "setka",
    "chotkasi": "chotka", "щётка": "chotka", "щетка": "chotka",
    "elak": "elak", "элак": "elak", "сито": "elak",
    "mishalka": "mishalka", "мишалка": "mishalka", "месилка": "mishalka",
    "shinnik": "shinnik", "шинник": "shinnik",
    "lenta": "lenta", "лента": "lenta",
    "bunker": "bunker", "бункер": "bunker",
    "vakum": "vakum", "вакум": "vakum", "вакуум": "vakum",
    "nasos": "nasos", "насос": "nasos", "nasosi": "nasos",
    "pichoq": "pichoq", "пичок": "pichoq", "нож": "pichoq",
    "katyoli": "katyol", "котёл": "katyol", "котел": "katyol", "kotyol": "katyol",
    "siklon": "siklon", "циклон": "siklon",
    "vinnik": "vinnik", "винник": "vinnik", "winnik": "vinnik",
    "ulitka": "ulitka", "улитка": "ulitka",
    "tross": "tross", "трос": "tross", "trossi": "tross",
    "kutargich": "kutargich", "ko'targich": "kutargich", "кутаргич": "kutargich",
    "salnik": "salnik", "salniki": "salnik", "salniklari": "salnik", "сальник": "salnik",
    "pachimnik": "pachimnik", "pachimligi": "pachimnik", "подшипник": "pachimnik",
    "podshipnik": "pachimnik", "пачимник": "pachimnik",
    "vali": "val", "вал": "val", "val": "val",
    "choynigi": "choynik", "choyniklari": "choynik", "чойник": "choynik",
    "sepi": "sep", "сеп": "sep",
    "rezinasi": "rezina", "резина": "rezina",
    "relesi": "rele", "реле": "rele",
    "chinti": "chinti", "чинти": "chinti",
    "pnevmatikasi": "pnevmo", "pinimatkasi": "pnevmo", "pnevmatika": "pnevmo",
    "un": "un", "ун": "un", "мука": "un", "suv": "suv", "сув": "suv", "вода": "suv",
    "tortgich": "tortgich", "tortadigan": "tortgich", "тортгич": "tortgich",
    "orqa": "orqa", "орка": "orqa", "katta": "katta", "kichkina": "kichkina",
    "par": "par", "пар": "par", "talkatori": "talkator", "толкатель": "talkator",
    "ramasi": "rama", "рама": "rama", "sikl": "siklon",
    # кириллицей пишут не реже, чем латиницей
    "мотори": "motor", "моторы": "motor", "матори": "motor", "мотар": "motor",
    "ремени": "remen", "ремни": "remen", "ременни": "remen", "ремен": "remen",
    "редуктори": "reduktor", "радуктор": "reduktor",
    "насоси": "nasos", "щёткаси": "chotka", "щеткаси": "chotka",
    "трасси": "tross", "троси": "tross", "вали": "val", "валик": "val",
    "чойниги": "choynik", "чойниклари": "choynik", "сепи": "sep",
    "резинаси": "rezina", "релеси": "rele", "рамаси": "rama",
    "толкатори": "talkator", "талкатори": "talkator", "катёли": "katyol",
    "кўтаргич": "kutargich", "кутаргич": "kutargich", "элаги": "elak",
    "пичоги": "pichoq", "пичоқ": "pichoq", "сикилон": "siklon",
    "пнематика": "pnevmo", "пневматика": "pnevmo", "пинематка": "pnevmo",
    "сальниклари": "salnik", "сальники": "salnik", "подшипники": "pachimnik",
    "тортадиган": "tortgich", "тортгич": "tortgich",
    "чоткаси": "chotka", "чотка": "chotka", "сетка": "setka", "сеткаси": "setka",
    "элак": "elak", "элакни": "elak", "мишалканинг": "mishalka", "шинникни": "shinnik",
    "лентани": "lenta", "бункерни": "bunker", "вакуми": "vakum", "унни": "un",
    "винник": "vinnik", "виник": "vinnik", "улитка": "ulitka", "улиткани": "ulitka",
    "чинтиси": "chinti", "паримат": "pnevmo", "катта": "katta", "кичкина": "kichkina",
    "орка": "orqa", "орқа": "orqa", "пачимлиги": "pachimnik", "пачимник": "pachimnik",
}


def norm(text: str) -> list:
    """Слова без апострофов и хвостов — чтобы «matori» и «мотор» встретились."""
    t = (text or "").lower().replace("'", "").replace("`", "").replace("ʻ", "")
    for ch in ".,;:!?()[]{}/\\\"\n\t-–—":
        t = t.replace(ch, " ")
    out = []
    for w in t.split():
        w = w.strip()
        if not w:
            continue
        out.append(ALIAS.get(w, w))
    return out


_KEYS = [(k, set(norm(n))) for k, n, _ in PARTS]


def match(text: str):
    """Что за узел написали. Возвращает ключ или None."""
    t = (text or "").strip()
    if not t:
        return None
    head = t.split()[0].strip(".)#№")
    if head.isdigit():                       # «12» или «12 sinmadi» — номер из списка
        i = int(head)
        if 1 <= i <= len(PARTS):
            return PARTS[i - 1][0]
    words = set(norm(t))
    if not words:
        return None
    best, score = None, 0
    for k, ws in _KEYS:
        hit = len(words & ws)
        if hit > score or (hit == score and best and hit and len(ws) < len(_KEYS[0][1])):
            best, score = k, hit
    if score >= 2:                           # два совпавших слова — уже уверенно
        return best
    if score == 1:                           # одно слово: годится, если оно ни с чем не путается
        for w in words:
            owners = [k for k, ws in _KEYS if w in ws]
            if len(owners) == 1:
                return owners[0]
    return None
