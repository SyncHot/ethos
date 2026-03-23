"""Tests for i18n translations."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer", "preboot"))

from i18n import I18N, CONFIRM_TOKENS


@pytest.fixture
def i18n():
    return I18N()


def test_polish_returns_key(i18n):
    assert i18n.t("Dalej", lang="pl") == "Dalej"


def test_english(i18n):
    assert i18n.t("Dalej", lang="en") == "Next"
    assert i18n.t("Hasło", lang="en") == "Password"


def test_german(i18n):
    assert i18n.t("Dalej", lang="de") == "Weiter"


def test_french(i18n):
    assert i18n.t("Dalej", lang="fr") == "Suivant"


def test_spanish(i18n):
    assert i18n.t("Dalej", lang="es") == "Siguiente"


def test_unknown_key(i18n):
    assert i18n.t("NoSuchKey", lang="en") == "NoSuchKey"


def test_get_all_en(i18n):
    tr = i18n.get_all("en")
    assert tr["Dalej"] == "Next"


def test_available_languages(i18n):
    langs = i18n.available_languages()
    codes = [l["code"] for l in langs]
    assert set(codes) == {"pl", "en", "de", "fr", "es"}


def test_confirm_tokens():
    assert CONFIRM_TOKENS["pl"] == "INSTALUJ"
    assert CONFIRM_TOKENS["en"] == "INSTALL"


def test_all_keys_translated(i18n):
    """Every key should have all 4 non-Polish translations."""
    for key in i18n.get_all("en"):
        for lang in ("en", "de", "fr", "es"):
            assert i18n.t(key, lang=lang), f"Missing {lang} for: {key}"
