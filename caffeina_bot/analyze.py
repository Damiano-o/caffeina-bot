"""Interpretazione del titolo e regole per decidere se uno sconto merita una notifica."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CAFFEINE_RE = re.compile(r"caffein|caffeine", re.IGNORECASE)

# "decaffeinato" contiene la parola caffeina: va neutralizzato prima del match
DECAF_RE = re.compile(r"decaffein\w*|decaf\b", re.IGNORECASE)
COFFEE_RE = re.compile(r"\bcaff[eè]\b|cialde|capsule di caff|caff[eè] in grani", re.IGNORECASE)

MG_RE = re.compile(r"(\d{2,4})\s*(?:mg|milligrammi)\b", re.IGNORECASE)
PILLS_RE = re.compile(
    r"(\d{2,4})\s*(?:compresse|capsule|caps|tavolette|tablet|pillole|pill|pastiglie)\b",
    re.IGNORECASE,
)

FORM_RE = re.compile(
    r"(?<![a-z])(compresse|capsule|caps|tavolette|tablet|pillole|pastiglie)(?![a-z])",
    re.IGNORECASE,
)

# Prodotti che non sono pillole di caffeina (caffè, bevande, integratori generici)
NOISE_RE = re.compile(
    r"cialde|capsule di caff[eè]|caff[eè] in grani|macinato|moka|espresso|"
    r"energy drink|bevanda|monodose solubile",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TitleInfo:
    mg_per_pill: float | None = None
    pills: int | None = None
    is_caffeine: bool = False
    is_noise: bool = False


def parse_title(title: str) -> TitleInfo:
    """Estrae mg per compressa e numero di compresse dal titolo Amazon."""
    info = TitleInfo()
    if not title:
        return info

    normalized = DECAF_RE.sub(" ", title)
    info.is_caffeine = bool(CAFFEINE_RE.search(normalized))
    info.is_noise = bool(COFFEE_RE.search(normalized) or NOISE_RE.search(normalized)) and not info.is_caffeine

    matches = [(int(m.group(1)), m.start()) for m in MG_RE.finditer(normalized)]
    if matches:
        # Preferisco il dosaggio che compare vicino alla parola "caffeina"
        caffeine_pos = [m.start() for m in CAFFEINE_RE.finditer(normalized)]
        scored = []
        for value, pos in matches:
            distance = min((abs(pos - c) for c in caffeine_pos), default=10_000)
            plausible = 50 <= value <= 600
            scored.append((0 if plausible else 1, distance, -value, value))
        scored.sort()
        info.mg_per_pill = float(scored[0][3])

    pills = [int(m.group(1)) for m in PILLS_RE.finditer(normalized)]
    if pills:
        info.pills = max(pills)  # il numero più grande è la confezione

    return info


def dedupe_key(title: str, pills: int | None, mg_per_pill: float | None) -> str:
    """Chiave stabile del prodotto, indipendente dall'ASIN.

    Usa le prime due parole (di solito marca + prodotto), la forma farmaceutica,
    la confezione e il dosaggio: le varianti dello stesso listing finiscono sulla
    stessa chiave, prodotti diversi no.
    """
    testo = DECAF_RE.sub(" ", title or "")
    tokens = re.findall(r"[a-z0-9]+", testo.lower())
    prefisso = " ".join(tokens[:2])
    forma = ""
    match = FORM_RE.search(testo)
    if match:
        forma = match.group(1).lower()[:4]
    mg = int(round(mg_per_pill)) if mg_per_pill else 0
    return f"{prefisso}|{forma}|{pills or 0}|{mg}"


def dose_cost(price: float | None, mg_per_pill: float | None, pills: int | None, dose_mg: float = 200.0) -> float | None:
    """Costo in euro di una dose da 200 mg di caffeina."""
    if not price or not mg_per_pill or not pills:
        return None
    if mg_per_pill <= 0 or pills <= 0:
        return None
    doses = pills * (mg_per_pill / dose_mg)
    if doses <= 0:
        return None
    return price / doses


@dataclass(slots=True)
class Verdict:
    alert: bool
    kind: str = ""  # folle | alto | calo | ""
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0
    effective_price: float | None = None
    dose_eur: float | None = None
    discount_pct: float | None = None

    @property
    def label(self) -> str:
        return {
            "folle": "SCONTO FOLLE",
            "alto": "SCONTO ALTO",
            "calo": "PREZZO AI MINIMI",
        }.get(self.kind, "OFFERTA")


def evaluate(
    *,
    price: float | None,
    list_price: float | None,
    discount_pct: float | None,
    coupon_pct: float | None,
    mg_per_pill: float | None,
    pills: int | None,
    available: bool,
    history_min: float | None,
    is_caffeine: bool,
    is_noise: bool,
    sconto_minimo: float,
    sconto_folle: float,
    dose_max_eur: float,
    calo_storico_minimo: float,
    solo_disponibili: bool = True,
    price_min_plausibile: float = 3.5,
    folle_fattore: float = 1.4,
) -> Verdict:
    """Applica le regole: notifica solo sconti alti e realmente convenienti.

    Una notifica scatta se:
      1. sconto >= sconto_minimo e costo/dose <= dose_max_eur  (affare solido)
      2. sconto >= sconto_folle e costo/dose <= dose_max_eur*1.4 (sconto enorme)
      3. prezzo <= minimo storico registrato - calo_storico_minimo%
    """
    verdict = Verdict(alert=False)

    if is_noise or not is_caffeine:
        verdict.reasons.append("prodotto non pertinente")
        return verdict
    if solo_disponibili and not available:
        verdict.reasons.append("non disponibile")
        return verdict
    if not price:
        verdict.reasons.append("prezzo non leggibile")
        return verdict
    if price < price_min_plausibile:
        verdict.reasons.append("prezzo sospetto (possibile errore o formato diverso)")
        return verdict
    if not pills or not mg_per_pill:
        verdict.reasons.append("confezione/dosaggio non riconosciuti")
        return verdict

    effective_price = price * (1 - (coupon_pct or 0) / 100)
    dose_eur = dose_cost(effective_price, mg_per_pill, pills)

    # lo sconto reale tiene conto anche del coupon applicato al checkout
    sconto_calcolato = None
    if list_price and list_price > effective_price:
        sconto_calcolato = round((list_price - effective_price) / list_price * 100, 1)
    if discount_pct is None:
        discount_pct = sconto_calcolato
    elif sconto_calcolato is not None:
        discount_pct = max(discount_pct, sconto_calcolato)

    verdict.effective_price = round(effective_price, 2)
    verdict.dose_eur = round(dose_eur, 4) if dose_eur else None
    verdict.discount_pct = discount_pct

    if dose_eur is None:
        verdict.reasons.append("costo per dose non calcolabile")
        return verdict

    sconto = discount_pct or 0.0
    kinds: list[str] = []

    if sconto >= sconto_folle and dose_eur <= dose_max_eur * folle_fattore:
        kinds.append("folle")
        verdict.reasons.append(f"sconto {sconto:.0f}% (soglia folle {sconto_folle:.0f}%)")
    elif sconto >= sconto_minimo and dose_eur <= dose_max_eur:
        kinds.append("alto")
        verdict.reasons.append(f"sconto {sconto:.0f}% e {dose_eur:.3f} €/dose")

    if history_min and price <= history_min * (1 - calo_storico_minimo / 100):
        kinds.append("calo")
        verdict.reasons.append(f"prezzo {price:.2f} € sotto il minimo storico {history_min:.2f} €")

    if not kinds:
        if sconto >= sconto_minimo:
            verdict.reasons.append(
                f"sconto {sconto:.0f}% ma {dose_eur:.3f} €/dose oltre il limite {dose_max_eur:.3f}"
            )
        else:
            verdict.reasons.append(f"sconto {sconto:.0f}% sotto la soglia {sconto_minimo:.0f}%")
        return verdict

    # Priorità: folle > alto > calo
    priority = {"folle": 3, "alto": 2, "calo": 1}
    verdict.kind = max(kinds, key=lambda k: priority[k])
    verdict.alert = True

    # Punteggio per ordinare gli alert: sconto + convenienza per dose
    dose_bonus = max(0.0, (dose_max_eur - dose_eur) / dose_max_eur) * 30 if dose_max_eur else 0
    verdict.score = round(min(100.0, sconto + dose_bonus), 1)
    return verdict
