# -*- coding: utf-8 -*-
"""
Utilitários simples de NLU/normalização para a Luna.

Objetivo: reconhecer o "formato" do projeto a partir de texto livre,
evitando reperguntas desnecessárias quando o usuário já deu a resposta
com variações (ex.: "era 3D", "quero 3-d/ia", "animação 3d", etc.).

Uso esperado no handler (exemplo):
    from fastapi_app.services.nlu import extract_formato

    formato = extract_formato(user_text)
    if formato is None:
        # fallback: reperguntar
    else:
        # seguir fluxo com 'formato'
"""

import re
import unicodedata
from typing import Optional

_CANONICOS = {
    "institucional": [
        r"institucional", r"institu(?:cional|cional)?", r"institucional(?:\s+de\s+marca)?"
    ],
    "3d/ia": [
        r"\b3\s*[-/]?\s*d\b", r"\b3d\s*/\s*ia\b", r"\bia\s*3d\b",
        r"animac(?:ao|ão)\s*3\s*[-/]?\s*d", r"\b3d\s*ia\b"
    ],
    "produto": [
        r"\bproduto(?:s)?\b", r"video\s*de\s*produto", r"apresenta[cç][aã]o\s*de\s*produto"
    ],
    "educativo": [
        r"\beducativo\b", r"\btutorial(?:es)?\b", r"\baula(?:s)?\b", r"\btreinamento\b"
    ],
    "convite": [
        r"\bconvite(?:s)?\b", r"\bconvite\s+digital\b"
    ],
    "homenagem": [
        r"\bhomenagem(?:s)?\b", r"\btributo\b"
    ],
}

# Palavras "ruído" que podem anteceder a resposta (ex.: "era 3D", "quero 3D")
_STOPWORDS_PREFIX = r"(?:era|e|é|eh|foi|quero|queria|pode\s*ser|seria|talvez|acho\s*que)\s+"

def _strip_accents(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", s)

def _norm(s: str) -> str:
    s = _strip_accents(s or "")
    s = s.lower()
    # uniformiza espaçamentos e hifens
    s = re.sub(r"[\u2010-\u2015]", "-", s)  # hifens unicode → "-"
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_formato(texto: str) -> Optional[str]:
    """
    Retorna o formato canônico se encontrar (ex.: "3d/ia", "produto", ...),
    ou None se não reconhecer.
    """
    t = _norm(texto)

    # tolerar ruído antes do termo (ex.: "era 3d", "quero 3d ia")
    t_noruido = re.sub(rf"^{_STOPWORDS_PREFIX}", "", t)

    candidatos = (t, t_noruido)
    for variante in candidatos:
        for can, padroes in _CANONICOS.items():
            for rx in padroes:
                if re.search(rx, variante, flags=re.IGNORECASE):
                    return can
    return None
