#!/usr/bin/env python3
"""
Fine-tune spaCy pl_core_news_lg NER on Polish medical PII data.
Generates synthetic training examples and trains the NER component.

Usage:
    source /opt/ethos/venv/bin/activate
    python /opt/ethos/tools/train_spacy_pii.py [--iterations 30]
"""

import argparse
import random
import os
import sys

MALE_FIRST = [
    'Adam', 'Adrian', 'Andrzej', 'Artur', 'Bartosz', 'Bogdan', 'Cezary',
    'Damian', 'Daniel', 'Dariusz', 'Dawid', 'Dominik', 'Edmund', 'Edward',
    'Filip', 'Franciszek', 'Gabriel', 'Grzegorz', 'Henryk', 'Hubert',
    'Igor', 'Ireneusz', 'Jacek', 'Jakub', 'Jan', 'Janusz',
    'Jerzy', 'Kamil', 'Karol', 'Kazimierz', 'Konrad', 'Krzysztof',
    'Leszek', 'Maciej', 'Marcin', 'Marek', 'Mariusz', 'Mateusz',
    'Norbert', 'Oskar', 'Piotr',
    'Rafal', 'Robert', 'Roman', 'Ryszard', 'Sebastian',
    'Stefan', 'Szymon', 'Tadeusz', 'Tomasz', 'Waldemar', 'Wiktor', 'Witold',
    'Wojciech', 'Zbigniew', 'Zygmunt',
]

FEMALE_FIRST = [
    'Agata', 'Agnieszka', 'Aleksandra', 'Alicja', 'Anna', 'Barbara',
    'Beata', 'Celina', 'Danuta', 'Dorota', 'Emilia',
    'Ewa', 'Ewelina', 'Gabriela', 'Halina', 'Hanna', 'Helena',
    'Ilona', 'Irena', 'Iwona', 'Izabela', 'Jadwiga', 'Joanna', 'Jolanta',
    'Julia', 'Justyna', 'Kamila', 'Karolina', 'Katarzyna', 'Klaudia',
    'Krystyna', 'Laura', 'Lidia', 'Magdalena', 'Maja',
    'Maria', 'Marlena', 'Marta', 'Michalina', 'Milena', 'Monika', 'Natalia',
    'Nina', 'Olga', 'Oliwia', 'Patrycja', 'Paulina', 'Regina', 'Renata',
    'Sandra', 'Stefania', 'Sylwia', 'Teresa', 'Urszula', 'Wanda', 'Weronika',
    'Wiktoria', 'Zofia',
]

MALE_SURNAMES = [
    'Kowalski', 'Nowak', 'Wisniewski', 'Wojcik', 'Kowalczyk', 'Kaminski',
    'Lewandowski', 'Zielinski', 'Szymanski', 'Wozniak', 'Dabrowski',
    'Kozlowski', 'Jankowski', 'Mazur', 'Kwiatkowski', 'Krawczyk',
    'Piotrowski', 'Grabowski', 'Nowakowski', 'Pawlowski', 'Michalski',
    'Adamczyk', 'Dudek', 'Zajac', 'Wieczorek', 'Krol',
    'Majewski', 'Olszewski', 'Jaworski', 'Malinowski',
    'Gorski', 'Rutkowski', 'Michalak', 'Sikora', 'Ostrowski', 'Baran',
    'Duda', 'Szewczyk', 'Tomaszewski', 'Pietrzak', 'Marciniak',
    'Wrobel', 'Zalewski', 'Jakubowski', 'Sadowski', 'Zawadzki',
    'Chmielewski', 'Sawicki', 'Sokolowski',
    'Kubiak', 'Maciejewski', 'Kucharski',
    'Wilk', 'Lis', 'Mazurek', 'Kalinowski', 'Wysocki', 'Adamski',
    'Wasilewski', 'Sobczak', 'Andrzejewski',
    'Glowacki', 'Zakrzewski', 'Sikorski', 'Krajewski',
    'Gajewski', 'Szymczak', 'Kozak', 'Pawlak', 'Kotecki', 'Filipiak',
]

FEMALE_SURNAMES = []
for _s in MALE_SURNAMES:
    if _s.endswith('ski'):
        FEMALE_SURNAMES.append(_s[:-1] + 'ka')
    elif _s.endswith('cki'):
        FEMALE_SURNAMES.append(_s[:-1] + 'ka')
    else:
        FEMALE_SURNAMES.append(_s)

COMPOUND_SURNAMES_F = [
    'Kowalska-Nowak', 'Wisniewska-Jozwiak', 'Zielinska-Marek',
    'Lewandowska-Krawczyk', 'Dabrowska-Sikora', 'Kaminska-Wojcik',
    'Szymanska-Grabowska', 'Nowakowska-Ziolek', 'Pawlowska-Baran',
    'Michalska-Duda', 'Olszewska-Pietrzak',
]


def decline_surname(surname):
    forms = {}
    if surname.endswith('ski'):
        stem = surname[:-1]
        forms['gen'] = stem + 'iego'
        forms['dat'] = stem + 'iemu'
        forms['inst'] = stem + 'im'
    elif surname.endswith('cki'):
        stem = surname[:-1]
        forms['gen'] = stem + 'iego'
        forms['dat'] = stem + 'iemu'
        forms['inst'] = stem + 'im'
    elif surname.endswith('ska'):
        stem = surname[:-1]
        forms['gen'] = stem + 'iej'
        forms['dat'] = stem + 'iej'
        forms['inst'] = surname[:-1] + 'a'
    elif surname.endswith('cka'):
        stem = surname[:-1]
        forms['gen'] = stem + 'iej'
        forms['dat'] = stem + 'iej'
        forms['inst'] = surname[:-1] + 'a'
    else:
        forms['gen'] = surname + 'a'
        forms['dat'] = surname + 'owi'
        forms['inst'] = surname + 'em'
    return forms


MEDICAL_TITLES = [
    'dr n. med.', 'dr hab. n. med.', 'prof. dr hab. n. med.',
    'prof.', 'dr hab.', 'dr', 'lek. med.', 'lek.',
]

FACILITIES = [
    'Szpital Uniwersytecki w Krakowie',
    'Szpital Kliniczny im. Barlickiego w Lodzi',
    'Szpital Miejski nr 2 w Poznaniu',
    'Szpital Wojewodzki w Gdansku',
    'Szpital Specjalistyczny w Katowicach',
    'Klinika Kardiologii IKEM',
    'Klinika Neurologii i Neurochirurgii',
    'Klinika Chorob Wewnetrznych',
    'Centrum Medyczne Damiana w Warszawie',
    'Centrum Zdrowia Matki Polki w Lodzi',
    'Centrum Onkologii w Gliwicach',
    'Poradnia Kardiologiczna',
    'Poradnia Neurologiczna',
    'Poradnia Diabetologiczna',
    'Instytut Kardiologii w Aninie',
    'Instytut Psychiatrii i Neurologii w Warszawie',
    'NZOZ Centrum Medyczne Medica',
    'SPZOZ Szpital Powiatowy w Minsku Mazowieckim',
    'Szpital MSWiA w Warszawie',
    'Szpital Praski w Warszawie',
    'Centrum Kardiologii Inwazyjnej w Kielcach',
    'Poradnia Zdrowia Psychicznego',
    'Centrum Medyczne HCP w Poznaniu',
]

STREETS = [
    'ul. Marszalkowska', 'ul. Zielona', 'ul. Krakowska', 'ul. Dluga',
    'ul. Pilsudskiego', 'ul. Kosciuszki', 'ul. Mickiewicza', 'ul. Slowackiego',
    'al. Solidarnosci', 'al. Jerozolimskie', 'os. Sportowe', 'os. Batorego',
    'pl. Wolnosci', 'ul. Wojska Polskiego', 'ul. Sienkiewicza', 'ul. Konopnickiej',
    'ul. Ogrodowa', 'ul. Lesna', 'ul. Polna', 'ul. Szkolna',
]

CITIES = [
    ('Warszawa', '00-001'), ('Krakow', '30-001'), ('Lodz', '90-001'),
    ('Wroclaw', '50-001'), ('Poznan', '60-001'), ('Gdansk', '80-001'),
    ('Katowice', '40-001'), ('Lublin', '20-001'), ('Rzeszow', '35-001'),
    ('Kielce', '25-001'), ('Bialystok', '15-001'), ('Szczecin', '70-001'),
    ('Bydgoszcz', '85-001'), ('Torun', '87-001'), ('Olsztyn', '10-001'),
    ('Elblag', '82-300'), ('Opole', '45-001'), ('Radom', '26-600'),
]

DIAGNOSES = [
    'nadcisnienie tetnicze', 'cukrzyca typu 2', 'migotanie przedsionkow',
    'niewydolnosc serca', 'choroba wiencowa', 'hipercholesterolemia',
    'astma oskrzelowa', 'przewlekla obturacyjna choroba pluc',
    'choroba Parkinsona', 'zespol Aspergera', 'choroba Hashimoto',
]

MEDICATIONS = [
    'amlodypina 5mg', 'metformina 850mg', 'atorwastatyna 20mg',
    'ramipril 5mg', 'bisoprolol 5mg', 'kwas acetylosalicylowy 75mg',
    'enalapryl 10mg', 'peryndopryl 5mg', 'walsartan 80mg', 'telmisartan 40mg',
    'losartan 50mg', 'kandesartan 8mg', 'lacydypina 4mg',
    'indapamid 1.5mg', 'torasemid 5mg', 'furosemid 40mg',
    'spironolakton 25mg', 'hydrochlorotiazyd 12.5mg',
    'klopidogrel 75mg', 'tikagrelol 90mg', 'warfaryna 3mg',
    'dabigatran 110mg', 'rywaroksaban 20mg', 'apiksaban 5mg',
    'digoksyna 0.25mg', 'amiodaron 200mg',
    'gliklazyd 30mg', 'empagliflozyna 10mg', 'dapagliflozyna 10mg',
    'semaglutyd 0.5mg', 'liraglutyd 1.2mg', 'insulina 20j',
    'ibuprofen 400mg', 'diklofenak 75mg', 'ketoprofen 100mg',
    'paracetamol 500mg', 'tramadol 50mg', 'metamizol 500mg',
    'amoksycylina 1g', 'azytromycyna 500mg', 'ciprofloksacyna 500mg',
    'doksycyklina 100mg', 'cefaleksyna 500mg',
    'salbutamol 100mcg', 'budezonid 200mcg', 'montelukast 10mg', 'teofilina 300mg',
    'omeprazol 20mg', 'pantoprazol 40mg', 'lansoprazol 30mg', 'esomeprazol 20mg',
    'escytalopram 10mg', 'sertralina 50mg', 'wenlafaksyna 75mg',
    'mirtazapina 30mg', 'olanzapina 5mg', 'kwetiapina 25mg',
    'lewotyroksyna 50mcg', 'prednizon 5mg', 'deksametazon 4mg',
    'heparyna 5000j', 'klindamycyna 300mg', 'amlorel 5mg',
    'finasteryd 5mg', 'Proscar 5mg', 'proscar 5mg',
]

MEDICAL_TERMS = [
    'nadcisnienie tetnicze', 'cukrzyca typu 2', 'migotanie przedsionkow',
    'niewydolnosc serca', 'choroba wiencowa', 'hipercholesterolemia',
    'astma oskrzelowa', 'przewlekla obturacyjna choroba pluc',
    'choroba Parkinsona', 'zespol Aspergera', 'choroba Hashimoto',
    'zatorowosc plucna', 'zawal miokardium', 'udar mozgu',
    'padaczka', 'stwardnienie rozsiane', 'miazdzyca',
    'zapalenie pluc', 'marskosc watroby', 'kamica nerkowa',
    'niedoczynnosc tarczycy', 'nadczynnosc tarczycy',
    'choroba Lesniowskiego-Crohna', 'wrzodziejace zapalenie jelita grubego',
    'reumatoidalne zapalenie stawow', 'osteoporoza',
    'przewlekla choroba nerek', 'zwezenie zastawki aortalnej',
    'kardiomiopatia rozstrzeniowa', 'tetniak aorty brzusznej',
    'zakrzepica zyl glebokich', 'niedokrwistosc z niedoboru zelaza',
    'hiperkaliemia', 'hiponatremia', 'kwasica metaboliczna',
    'zespol metaboliczny', 'obrzek pluc', 'odma oplucnowa',
]

PROCEDURES = [
    'gastroskopia', 'kolonoskopia', 'ultrasonografia jamy brzusznej',
    'tomografia komputerowa', 'rezonans magnetyczny', 'echokardiografia',
    'koronarografia', 'endoskopia', 'biopsja', 'laparoskopia',
    'artroskopia', 'hemodializa', 'chemioterapia', 'radioterapia',
    'ablacja', 'angioplastyka', 'Holter EKG', 'EKG spoczynkowe',
    'RTG klatki piersiowej', 'spirometria', 'cystoskopia',
    'badanie histopatologiczne', 'punkcja ledzwiowa',
    'USG Doppler', 'ECHO serca', 'proktoskopia',
    'test wysilkowy', 'scyntygrafia', 'bronchoskopia',
    'dializoterapia', 'fizykoterapia', 'rehabilitacja kardiologiczna',
]

LAB_TESTS = [
    'morfologia krwi', 'CRP', 'OB', 'TSH', 'fT3', 'fT4',
    'glukoza na czczo', 'HbA1c', 'kreatynina', 'mocznik',
    'ALT', 'AST', 'GGTP', 'bilirubina calkowita', 'bilirubina bezposrednia',
    'hemoglobina', 'hematokryt', 'leukocyty', 'trombocyty', 'erytrocyty',
    'cholesterol calkowity', 'cholesterol LDL', 'cholesterol HDL',
    'triglicerydy', 'kwas moczowy', 'sod', 'potas', 'wapn',
    'albuminy', 'bialko calkowite', 'INR', 'APTT', 'D-dimery',
    'prokalcytonina', 'ferrytyna', 'zelazo', 'witamina D3',
    'witamina B12', 'kwas foliowy', 'PSA', 'CEA', 'AFP',
    'gazometria', 'badanie ogolne moczu', 'posiew krwi',
]


def rn(lst):
    return random.choice(lst)


def rand_num(a, b):
    return str(random.randint(a, b))


def rand_pesel():
    return ''.join([str(random.randint(0, 9)) for _ in range(11)])


def rand_phone():
    fmts = [
        lambda: f"+48 {rand_num(500,899)} {rand_num(100,999)} {rand_num(100,999)}",
        lambda: f"{rand_num(500,899)}-{rand_num(100,999)}-{rand_num(100,999)}",
        lambda: f"{rand_num(500,899)} {rand_num(100,999)} {rand_num(100,999)}",
    ]
    return random.choice(fmts)()


def rand_date():
    d = random.randint(1, 28)
    m = random.randint(1, 12)
    y = random.randint(2018, 2026)
    return f"{d:02d}.{m:02d}.{y}"


def rand_address():
    street = rn(STREETS)
    num = rand_num(1, 150)
    apt = f"/{rand_num(1, 30)}" if random.random() > 0.5 else ""
    city, code = rn(CITIES)
    return f"{street} {num}{apt}, {code} {city}"


def make_name():
    if random.random() < 0.5:
        return rn(MALE_FIRST), rn(MALE_SURNAMES)
    fn = rn(FEMALE_FIRST)
    sn = rn(FEMALE_SURNAMES)
    if random.random() < 0.15:
        sn = rn(COMPOUND_SURNAMES_F)
    return fn, sn


def make_doctor():
    title = rn(MEDICAL_TITLES)
    first, surname = make_name()
    return title, first, surname


# --- Template generators ---
# Each returns (text, entities) where entities = [(start, end, label)]

def _fill(template, replacements):
    """Fill template and compute entity spans."""
    text = template
    entities = []
    for placeholder, value, label in replacements:
        if label is None:
            text = text.replace(placeholder, value, 1)
            continue
        idx = text.find(placeholder)
        if idx < 0:
            text = text.replace(placeholder, value, 1)
            continue
        text = text[:idx] + value + text[idx + len(placeholder):]
        entities.append((idx, idx + len(value), label))
    entities.sort(key=lambda e: e[0])
    return text, entities


def gen_patient_header():
    first, surname = make_name()
    name = f"{first} {surname}"
    pesel = rand_pesel()
    date = rand_date()
    templates = [
        ("Pacjent {N}, PESEL {P}, przyjety na oddzial.",
         [("{N}", name, "persName"), ("{P}", pesel, None)]),
        ("Dane pacjenta: {N}, nr PESEL: {P}.",
         [("{N}", name, "persName"), ("{P}", pesel, None)]),
        ("Pacjentka {N} zglosila sie do poradni z powodu bolow glowy.",
         [("{N}", name, "persName")]),
        ("Imie i nazwisko: {N}. PESEL: {P}. Data wizyty: {D}.",
         [("{N}", name, "persName"), ("{P}", pesel, None), ("{D}", date, None)]),
        ("Karta informacyjna pacjenta {N}, numer PESEL {P}.",
         [("{N}", name, "persName"), ("{P}", pesel, None)]),
        ("{N}, lat {A}, przyjeta na oddzial wewnetrzny.",
         [("{N}", name, "persName"), ("{A}", rand_num(20, 90), None)]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_patient_with_facility():
    first, surname = make_name()
    name = f"{first} {surname}"
    facility = rn(FACILITIES)
    pesel = rand_pesel()
    templates = [
        ("{N}, lat {A}, przyjeta do {F}.",
         [("{N}", name, "persName"), ("{A}", rand_num(20, 90), None),
          ("{F}", facility, "orgName")]),
        ("Pacjent {N} zostal przyjety do {F} z rozpoznaniem {DG}.",
         [("{N}", name, "persName"), ("{F}", facility, "orgName"),
          ("{DG}", rn(DIAGNOSES), None)]),
        ("Wypisano {N} z {F} w stanie ogolnym dobrym.",
         [("{N}", name, "persName"), ("{F}", facility, "orgName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_doctor_mention():
    title, first, surname = make_doctor()
    doctor = f"{title} {first} {surname}"
    templates = [
        ("Lekarz prowadzacy: {D}.",
         [("{D}", doctor, "persName")]),
        ("Badanie zlecil {D}.",
         [("{D}", doctor, "persName")]),
        ("Konsultacja z {D} w sprawie dalszego leczenia.",
         [("{D}", doctor, "persName")]),
        ("Operacje przeprowadzil {D} z zespolem.",
         [("{D}", doctor, "persName")]),
        ("Wyniki konsultacji {D}: brak przeciwwskazan.",
         [("{D}", doctor, "persName")]),
        ("Podpis lekarza: {D}",
         [("{D}", doctor, "persName")]),
        ("Wizyta kontrolna u {D} za 3 miesiace.",
         [("{D}", doctor, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_facility_mention():
    facility = rn(FACILITIES)
    templates = [
        ("Pacjent zostal przyjety do {F}.",
         [("{F}", facility, "orgName")]),
        ("Wypisano z {F} w stanie ogolnym dobrym.",
         [("{F}", facility, "orgName")]),
        ("Skierowanie do {F} celem dalszej diagnostyki.",
         [("{F}", facility, "orgName")]),
        ("Badanie wykonano w {F}.",
         [("{F}", facility, "orgName")]),
        ("Leczenie kontynuowane w {F}.",
         [("{F}", facility, "orgName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_declined_name():
    first, surname = make_name()
    declined = decline_surname(surname)
    case_form = rn(list(declined.values()))
    templates = [
        ("Wyniki badan {S} wskazuja na poprawe.",
         [("{S}", case_form, "persName")]),
        ("Skierowano {S} do specjalisty.",
         [("{S}", case_form, "persName")]),
        ("W dokumentacji {F} {S} odnotowano.",
         [("{F}", first, "persName"), ("{S}", case_form, "persName")]),
        ("Pacjenta {S} wypisano ze szpitala.",
         [("{S}", case_form, "persName")]),
        ("Badania laboratoryjne {S} w normie.",
         [("{S}", case_form, "persName")]),
        ("Na wniosek {S} wykonano dodatkowe badanie USG.",
         [("{S}", case_form, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_all_caps_name():
    first, surname = make_name()
    caps_name = f"{surname.upper()} {first.upper()}"
    templates = [
        ("{N}, ur. {D}, PESEL: {P}",
         [("{N}", caps_name, "persName"), ("{D}", rand_date(), None), ("{P}", rand_pesel(), None)]),
        ("Pacjent: {N}",
         [("{N}", caps_name, "persName")]),
        ("{N} - Karta Informacyjna Leczenia Szpitalnego",
         [("{N}", caps_name, "persName")]),
        ("NAZWISKO I IMIE: {N}",
         [("{N}", caps_name, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_surname_first():
    first, surname = make_name()
    name = f"{surname} {first}"
    templates = [
        ("{N}, adres zamieszkania: {A}.",
         [("{N}", name, "persName"), ("{A}", rand_address(), "geogName")]),
        ("Nazwisko i imie: {N}.",
         [("{N}", name, "persName")]),
        ("{N} - wyniki badan laboratoryjnych.",
         [("{N}", name, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_family_mention():
    first1, sur1 = make_name()
    first2, sur2 = make_name()
    patient = f"{first1} {sur1}"
    family = f"{first2} {sur2}"
    rel = rn(['Syn', 'Corka', 'Zona', 'Maz', 'Matka', 'Ojciec', 'Siostra', 'Brat'])
    templates = [
        ("Pacjent {P}. {R}: {F}.",
         [("{P}", patient, "persName"), ("{R}", rel, None), ("{F}", family, "persName")]),
        ("{R} pacjenta: {F}. Telefon kontaktowy: {T}.",
         [("{R}", rel, None), ("{F}", family, "persName"), ("{T}", rand_phone(), None)]),
        ("Opiekun prawny: {F}. Pacjent: {P}.",
         [("{F}", family, "persName"), ("{P}", patient, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_complex_sentence():
    first, surname = make_name()
    patient = f"{first} {surname}"
    title, dfirst, dsurname = make_doctor()
    doctor = f"{title} {dfirst} {dsurname}"
    facility = rn(FACILITIES)
    diagnosis = rn(DIAGNOSES)
    med = rn(MEDICATIONS)
    templates = [
        ("Pacjent {P} zostal przyjety do {F} z rozpoznaniem {DG}. Lekarz prowadzacy: {D}.",
         [("{P}", patient, "persName"), ("{F}", facility, "orgName"),
          ("{DG}", diagnosis, None), ("{D}", doctor, "persName")]),
        ("{D} z {F} skierowal pacjenta {P} na badania dodatkowe.",
         [("{D}", doctor, "persName"), ("{F}", facility, "orgName"),
          ("{P}", patient, "persName")]),
        ("W dniu {DT} {P} zglosil sie do {F}. Przyjety przez {D}.",
         [("{DT}", rand_date(), None), ("{P}", patient, "persName"),
          ("{F}", facility, "orgName"), ("{D}", doctor, "persName")]),
        ("{P} wypisany z {F}. Zalecenia: {M}, kontrola za 3 miesiace u {D}.",
         [("{P}", patient, "persName"), ("{F}", facility, "orgName"),
          ("{M}", med, None), ("{D}", doctor, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_negative_medical():
    templates = [
        "Rozpoznano chorobe Parkinsona i zalecono dalsza diagnostyke.",
        "Pacjent z zespolem Aspergera, pod opieka psychologa.",
        "W wywiadzie choroba Hashimoto leczona lewotyroksyna.",
        "Stwierdzono zespol Marfana - skierowanie do genetyka.",
        "Test Romberga ujemny. Objaw Babinskiego nieobecny.",
        "Skala Barthel: 85 punktow. Skala Glasgow: 15.",
        "Objaw Chvostka dodatni. Objaw Trousseau ujemny.",
        "W badaniu echokardiograficznym zastawka aortalna prawidlowa.",
        "Wyniki badan laboratoryjnych: morfologia w normie, CRP ponizej normy.",
        "Zalecenia: dieta niskosodowa, aktywnosc fizyczna 30 min dziennie.",
        "EKG: rytm zatokowy miarowy, 72/min, bez cech niedokrwienia.",
        "RTG klatki piersiowej: bez zmian ogniskowych.",
        "Holter EKG: pojedyncze pobudzenia nadkomorowe, bez pauz.",
        "Rozpoznanie: choroba Lesniowskiego-Crohna w remisji klinicznej.",
    ]
    return rn(templates), []


def gen_negative_drugs():
    """Drug-heavy text — NO entities at all."""
    med1, med2, med3 = random.sample(MEDICATIONS, 3)
    templates = [
        f"Dawkowanie: {med1} raz dziennie, {med2} wieczorem, {med3} po posilek.",
        f"Zalecenia farmakologiczne: 1) {med1} 2) {med2} 3) {med3}.",
        f"Kontynuowac leczenie: {med1}, {med2}. Odstawic {med3}.",
        f"Recepta: {med1}, {med2}, {med3}. Kontrola za 3 miesiace.",
        f"Leki stale: {med1} rano, {med2} w poludnie, {med3} wieczorem.",
        f"Zmieniono dawke {med1} na {med2}. Dolaczono {med3}.",
        f"Farmakoterapia: {med1} 1x dz., {med2} 2x dz., {med3} w razie potrzeby.",
        f"Stosowac {med1} pod kontrola cisnienia. {med2} pod kontrola glikemii.",
    ]
    return rn(templates), []


def gen_negative_lab():
    """Lab results — NO entities at all."""
    tests = random.sample(LAB_TESTS, min(5, len(LAB_TESTS)))
    val = lambda: f"{random.uniform(0.5, 200):.1f}"
    templates = [
        f"Wyniki badan: {tests[0]} {val()}, {tests[1]} {val()}, {tests[2]} {val()}.",
        f"Badania laboratoryjne: {tests[0]} - norma, {tests[1]} - podwyzszone, {tests[2]} - w normie.",
        f"Kontrolne badania krwi: {tests[0]} {val()} (N: 4.0-10.0), {tests[1]} {val()}.",
        f"{tests[0]}: {val()}, {tests[1]}: {val()}, {tests[2]}: {val()}, {tests[3]}: {val()}.",
        f"Morfologia: WBC {val()} tys/uL, RBC {val()} mln/uL, Hgb {val()} g/dL, PLT {val()} tys/uL.",
        f"Panel lipidowy: cholesterol calkowity {val()}, LDL {val()}, HDL {val()}, TG {val()}.",
        f"Biochemia: {tests[0]} {val()}, {tests[1]} {val()}. Gazometria: pH 7.{random.randint(30,45)}.",
    ]
    return rn(templates), []


def gen_negative_diagnosis():
    """Diagnosis text with ICD codes — NO entities at all."""
    diag1, diag2 = random.sample(MEDICAL_TERMS, 2)
    proc = rn(PROCEDURES)
    icd1 = f"{rn('IEJKMN')}{random.randint(10,99)}.{random.randint(0,9)}"
    icd2 = f"{rn('IEJKMN')}{random.randint(10,99)}"
    templates = [
        f"Rozpoznanie: {icd1} {diag1}, {icd2} {diag2}.",
        f"Rozpoznanie glowne: {diag1}. Wspolistniejace: {diag2}.",
        f"Wykonano {proc}. Wynik: {diag1} w fazie remisji.",
        f"Skierowanie na {proc} z powodu {diag1}.",
        f"Epikryza: Pacjent z {diag1} i {diag2}. Wykonano {proc}.",
        f"Rozpoznanie wg ICD-10: {icd1} - {diag1}. Powiklania: brak.",
        f"W trakcie hospitalizacji leczono {diag1}. Wypisano w stanie stabilnym.",
        f"Wywiad: {diag1} od 5 lat, {diag2} od 2 lat. Leczony ambulatoryjnie.",
    ]
    return rn(templates), []


def gen_discharge_summary():
    """Discharge-style text mixing entities with medical terms."""
    first, surname = make_name()
    patient = f"{first} {surname}"
    facility = rn(FACILITIES)
    diag = rn(MEDICAL_TERMS)
    med1, med2 = random.sample(MEDICATIONS, 2)
    proc = rn(PROCEDURES)
    templates = [
        ("Pacjent {P} wypisany z {F}. Rozpoznanie: {DG}. Wykonano {PR}. Zalecenia: {M1}, {M2}.",
         [("{P}", patient, "persName"), ("{F}", facility, "orgName"),
          ("{DG}", diag, None), ("{PR}", proc, None), ("{M1}", med1, None), ("{M2}", med2, None)]),
        ("Karta informacyjna leczenia szpitalnego. Pacjent: {P}. Oddział: {F}. Rozpoznanie: {DG}. Leki: {M1}, {M2}.",
         [("{P}", patient, "persName"), ("{F}", facility, "orgName"),
          ("{DG}", diag, None), ("{M1}", med1, None), ("{M2}", med2, None)]),
        ("{P} hospitalizowany w {F}. {PR} bez powiklan. {DG} w remisji. Leczenie: {M1}, {M2}.",
         [("{P}", patient, "persName"), ("{F}", facility, "orgName"),
          ("{PR}", proc, None), ("{DG}", diag, None), ("{M1}", med1, None), ("{M2}", med2, None)]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_maiden_name():
    first = rn(FEMALE_FIRST)
    married = rn(FEMALE_SURNAMES)
    maiden = rn(FEMALE_SURNAMES)
    while maiden == married:
        maiden = rn(FEMALE_SURNAMES)
    full = f"{first} {married}"
    maiden_part = f"z d. {maiden}" if random.random() > 0.5 else f"z domu {maiden}"
    templates = [
        ("{N} ({M}), przyjeta na oddzial.",
         [("{N}", full, "persName"), ("{M}", maiden_part, "persName")]),
        ("Pacjentka {N}, {M}, PESEL {P}.",
         [("{N}", full, "persName"), ("{M}", maiden_part, "persName"), ("{P}", rand_pesel(), None)]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_abbreviation_name():
    first, surname = make_name()
    abbrev = f"{first[0]}. {surname}"
    templates = [
        ("Lekarz dyzurny: {N}.",
         [("{N}", abbrev, "persName")]),
        ("Podpis: {N}",
         [("{N}", abbrev, "persName")]),
        ("Konsultacja {N} - bez uwag.",
         [("{N}", abbrev, "persName")]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_address_context():
    first, surname = make_name()
    name = f"{first} {surname}"
    addr = rand_address()
    templates = [
        ("Pacjent {N}, zamieszakly: {A}.",
         [("{N}", name, "persName"), ("{A}", addr, "geogName")]),
        ("Adres zamieszkania: {A}. Pacjent: {N}.",
         [("{A}", addr, "geogName"), ("{N}", name, "persName")]),
        ("{N}, {A}, tel. {T}.",
         [("{N}", name, "persName"), ("{A}", addr, "geogName"), ("{T}", rand_phone(), None)]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


def gen_negative_pwz():
    """PWZ (prawo wykonywania zawodu) context — NO entities. PWZ is public, not PII."""
    pwz_num = str(random.randint(1000000, 9999999))
    first, surname = make_name()
    doctor = f"{first} {surname}"
    spec = rn(['neurolog', 'kardiolog', 'ortopeda', 'internista', 'chirurg',
               'radiolog', 'okulista', 'dermatolog', 'pediatra', 'ginekolog',
               'urolog', 'onkolog', 'pulmonolog', 'gastroenterolog', 'endokrynolog'])
    templates = [
        (f"Lekarz {{N}}, nr PWZ {pwz_num}, specjalista {spec}.",
         [('{N}', doctor, 'persName')]),
        (f"Podpis: {{N}}, PWZ: {pwz_num}",
         [('{N}', doctor, 'persName')]),
        (f"{{N}} (PWZ {pwz_num}) - {spec}. Konsultacja bez uwag.",
         [('{N}', doctor, 'persName')]),
        (f"Lekarz prowadzacy: {{N}}, prawo wykonywania zawodu nr {pwz_num}.",
         [('{N}', doctor, 'persName')]),
        (f"Pieczatka: {{N}}, lek. {spec}, PWZ {pwz_num}.",
         [('{N}', doctor, 'persName')]),
        (f"nr PWZ: {pwz_num}. Lekarz {{N}}, specjalista {spec}.",
         [('{N}', doctor, 'persName')]),
        (f"Badanie wykonal {{N}}, nr prawa wykonywania zawodu: {pwz_num}.",
         [('{N}', doctor, 'persName')]),
        (f"Wynik autoryzowany przez {{N}} (PWZ: {pwz_num}), {spec}.",
         [('{N}', doctor, 'persName')]),
    ]
    tpl, reps = rn(templates)
    return _fill(tpl, reps)


GENERATORS = [
    (gen_patient_header, 80),
    (gen_patient_with_facility, 60),
    (gen_doctor_mention, 60),
    (gen_facility_mention, 50),
    (gen_declined_name, 50),
    (gen_all_caps_name, 40),
    (gen_surname_first, 30),
    (gen_family_mention, 30),
    (gen_complex_sentence, 60),
    (gen_negative_medical, 100),
    (gen_negative_drugs, 120),
    (gen_negative_lab, 80),
    (gen_negative_diagnosis, 80),
    (gen_discharge_summary, 60),
    (gen_maiden_name, 20),
    (gen_abbreviation_name, 20),
    (gen_address_context, 30),
    (gen_negative_pwz, 80),
]


def generate_dataset(n_examples):
    pool = []
    for gen_fn, weight in GENERATORS:
        pool.extend([gen_fn] * weight)

    data = []
    seen = set()
    attempts = 0
    while len(data) < n_examples and attempts < n_examples * 5:
        attempts += 1
        gen_fn = random.choice(pool)
        text, entities = gen_fn()
        if text in seen:
            continue
        seen.add(text)

        valid = True
        for start, end, label in entities:
            if start < 0 or end > len(text) or start >= end:
                valid = False
                break
        if valid and len(entities) > 1:
            for i in range(len(entities) - 1):
                if entities[i][1] > entities[i + 1][0]:
                    valid = False
                    break
        if valid:
            data.append((text, {"entities": entities}))
    return data


def train_model(train_data, dev_data, base_model, output_dir, n_iter=30):
    import spacy
    from spacy.training import Example
    from spacy.util import minibatch, compounding

    print(f"Loading base model: {base_model}")
    nlp = spacy.load(base_model)
    ner = nlp.get_pipe("ner")

    for _, annotations in train_data:
        for ent in annotations.get("entities", []):
            ner.add_label(ent[2])

    pipe_exceptions = ["ner"]
    other_pipes = [pipe for pipe in nlp.pipe_names if pipe not in pipe_exceptions]

    print(f"Training NER with {len(train_data)} examples for {n_iter} iterations")
    print(f"Disabling pipes: {other_pipes}")

    best_f1 = 0.0
    best_iter = 0

    with nlp.disable_pipes(*other_pipes):
        optimizer = nlp.resume_training()

        for iteration in range(n_iter):
            random.shuffle(train_data)
            losses = {}
            batches = minibatch(train_data, size=compounding(4.0, 32.0, 1.001))

            for batch in batches:
                examples = []
                for text, annots in batch:
                    doc = nlp.make_doc(text)
                    example = Example.from_dict(doc, annots)
                    examples.append(example)
                nlp.update(examples, sgd=optimizer, losses=losses)

            if dev_data and (iteration + 1) % 5 == 0:
                scores = evaluate(nlp, dev_data)
                f1 = scores.get('ents_f', 0)
                print(f"  Iter {iteration+1:3d}: loss={losses.get('ner', 0):.4f}, "
                      f"P={scores.get('ents_p', 0):.2f}, "
                      f"R={scores.get('ents_r', 0):.2f}, "
                      f"F1={f1:.2f}")
                if f1 > best_f1:
                    best_f1 = f1
                    best_iter = iteration + 1
                    nlp.to_disk(output_dir)
                    print(f"  * New best F1={f1:.2f} - saved to {output_dir}")
            else:
                print(f"  Iter {iteration+1:3d}: loss={losses.get('ner', 0):.4f}")

    if not dev_data or best_f1 == 0:
        nlp.to_disk(output_dir)
        print(f"Saved final model to {output_dir}")
    else:
        print(f"\nBest model at iteration {best_iter} with F1={best_f1:.2f}")

    return nlp


def evaluate(nlp, data):
    from spacy.training import Example
    from spacy.scorer import Scorer

    examples = []
    for text, annots in data:
        doc = nlp.make_doc(text)
        example = Example.from_dict(doc, annots)
        pred = nlp(text)
        example_eval = Example(pred, example.reference)
        examples.append(example_eval)

    scorer = Scorer()
    scores = scorer.score(examples)
    return {
        'ents_p': scores.get('ents_p', 0) * 100,
        'ents_r': scores.get('ents_r', 0) * 100,
        'ents_f': scores.get('ents_f', 0) * 100,
    }


def run_demo(model_path):
    import spacy
    nlp = spacy.load(model_path)
    tests = [
        "Pacjent Jan Kowalski, PESEL 80010112345, zamieszakly ul. Zielona 12, 00-001 Warszawa.",
        "Lekarz prowadzacy: dr hab. n. med. Maria Nowicka-Jozwiak.",
        "Szpital Kliniczny im. Barlickiego w Lodzi, Oddzial Kardiologii.",
        "KOTECKI Mateusz, ur. 15.03.1985, adres: os. Sportowe 7/3, 31-234 Krakow.",
        "Rozpoznano chorobe Parkinsona i zespol Aspergera.",
        "Skierowano Kowalskiego do neurologa.",
        "Wypisano z Centrum Medycznego Damiana w Warszawie.",
        "Syn: Tomasz Wisniewski. Zona: Anna Wisniewska z d. Kowalska.",
        "Badanie zlecil dr n. med. Katarzyna Nowakowska-Ziolek.",
        "K. Lewandowski - konsultacja kardiologiczna bez uwag.",
    ]
    print("\n=== Demo Inference ===")
    for t in tests:
        doc = nlp(t)
        ents = [(e.text, e.label_) for e in doc.ents]
        print(f"  {t[:80]}")
        print(f"    -> {ents}")
    print()


def main():
    parser = argparse.ArgumentParser(description='Fine-tune spaCy NER for Polish PII')
    parser.add_argument('--iterations', type=int, default=50)
    parser.add_argument('--output', default='/opt/ethos/data/models/spacy_pii_pl')
    parser.add_argument('--base-model', default='pl_core_news_lg')
    parser.add_argument('--train-size', type=int, default=1500)
    parser.add_argument('--dev-size', type=int, default=300)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--demo-only', action='store_true')
    args = parser.parse_args()

    if args.demo_only:
        run_demo(args.output)
        return

    random.seed(args.seed)

    print(f"Generating {args.train_size} training + {args.dev_size} dev examples...")
    train_data = generate_dataset(args.train_size)
    random.seed(args.seed + 1)
    dev_data = generate_dataset(args.dev_size)
    print(f"Generated {len(train_data)} train, {len(dev_data)} dev examples")

    print("\nSample training examples:")
    for text, annots in train_data[:3]:
        print(f"  TEXT: {text[:100]}")
        for s, e, l in annots['entities']:
            print(f"    [{s}:{e}] {text[s:e]} -> {l}")

    os.makedirs(args.output, exist_ok=True)

    model = train_model(train_data, dev_data, args.base_model, args.output, args.iterations)

    print("\n=== Final Evaluation on Dev Set ===")
    scores = evaluate(model, dev_data)
    print(f"  Precision: {scores['ents_p']:.2f}%")
    print(f"  Recall:    {scores['ents_r']:.2f}%")
    print(f"  F1:        {scores['ents_f']:.2f}%")

    run_demo(args.output)
    print("Done!")


if __name__ == '__main__':
    main()
