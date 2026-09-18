"""Hand-written Tagalog/Taglish G2P -> IPA subset of LA-Multilingual's 70-phone inventory.

Tagalog orthography is nearly phonemic, so rule-based G2P beats espeak surrogate
languages (id/ms). English loanwords (Taglish) handled via digraph rules that are
unambiguous because those sequences never occur in native Tagalog words
(ee, oo, ou, th, sh, ch, ck, ph only appear in English loans).
"""
import re

# IPA symbols must match data.py phone_dict exactly
G2P_DIGRAPHS = {
    "ng": ["ŋ"], "ngg": ["ŋ", "ɡ"],
    # English loans (sequences impossible in native Tagalog)
    "sh": ["ʃ"], "ch": ["tʃ"], "ck": ["k"], "th": ["θ"], "ph": ["f"],
    "ee": ["iː"], "oo": ["uː"], "ou": ["aʊ"], "oa": ["oʊ"],
    "qu": ["k", "w"], "ss": ["s"], "ll": ["l"], "tt": ["t"], "pp": ["p"],
    "rr": ["ɾ"], "nn": ["n"], "mm": ["m"], "ff": ["f"], "bb": ["b"], "dd": ["d"], "gg": ["ɡ"],
}

SINGLE = {
    "a": ["a"], "e": ["e"], "i": ["i"], "o": ["o"], "u": ["u"],
    "b": ["b"], "k": ["k"], "d": ["d"], "g": ["ɡ"], "h": ["h"],
    "l": ["l"], "m": ["m"], "n": ["n"], "p": ["p"], "r": ["ɾ"],
    "s": ["s"], "t": ["t"], "w": ["w"], "y": ["j"],
    # Spanish-loan letters
    "c": None,  # context: k before a/o/u, s before e/i
    "ñ": ["ɲ"], "j": ["dʒ"], "v": ["b"], "f": ["f"], "z": ["s"],
    "x": ["k", "s"], "q": ["k"],
}

# diphthong glides after vowels: ay->aɪ, aw->aʊ, oy->oɪ, uy, iw, ey, ew
DIPHTHONG = {
    "ay": ["aɪ"], "aw": ["aʊ"], "oy": ["ɔɪ"], "uy": ["ʊɪ"],
    "iw": ["iʊ"], "ey": ["eɪ"], "ew": ["eʊ"], "iy": ["iː"], "uw": ["uː"],
}

SPECIAL_WORDS = {
    "mga": ["m", "a", "ŋ", "a"],
    "pala": ["p", "a", "l", "a"],
}


def _clean(word: str) -> str:
    w = word.lower().strip()
    w = re.sub(r"[^\w' -]", "", w, flags=re.UNICODE)
    return w


def tagalog_g2p(word: str) -> list[str]:
    """Word -> list of IPA phone strings (inventory-compatible)."""
    w = _clean(word)
    if not w:
        return []
    if w in SPECIAL_WORDS:
        return SPECIAL_WORDS[w]
    phones: list[str] = []
    i, n = 0, len(w)
    while i < n:
        c = w[i]
        two = w[i:i+2]
        # diphthong glide (only when vowel is followed by glide at syllable end;
        # simple heuristic: apply when next-next char is not a vowel)
        if two in DIPHTHONG and (i + 2 >= n or w[i+2] not in "aeiou"):
            phones += DIPHTHONG[two]; i += 2; continue
        if two == "ng":
            # "ng" between two vowels splits n.g (e.g. "mang-aalam" no; "singe"?) -
            # in Tagalog intervocalic "ng" is always ŋ (e.g. "bango"), keep ŋ
            phones += ["ŋ"]; i += 2; continue
        if two in G2P_DIGRAPHS:
            phones += G2P_DIGRAPHS[two]; i += 2; continue
        if c == "c":
            nxt = w[i+1] if i + 1 < n else ""
            phones += ["s"] if nxt in "ei" else ["k"]; i += 1; continue
        if c == "g":
            nxt = w[i+1] if i + 1 < n else ""
            if nxt in "ei":
                phones += ["dʒ"]; i += 1; continue
            phones += ["ɡ"]; i += 1; continue
        if c in SINGLE and SINGLE[c]:
            phones += SINGLE[c]; i += 1; continue
        if c == "'":
            i += 1; continue  # elision marker
        if c == "-":
            phones.append(" "); i += 1; continue  # hyphen = small gap
        # unknown char (digit etc.) -> skip
        i += 1
    return phones or ["ə"]
