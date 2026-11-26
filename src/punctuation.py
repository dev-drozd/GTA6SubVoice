from __future__ import annotations

import re

TERMINAL_PUNCT = (".", "!", "?", "…")
CONTINUATION_PUNCT = (",", ";", ":", "–", "—")

QUESTION_WORDS = {
    "кто",
    "что",
    "где",
    "куда",
    "откуда",
    "когда",
    "почему",
    "зачем",
    "как",
    "какой",
    "какая",
    "какое",
    "какие",
    "какого",
    "какую",
    "каких",
    "чей",
    "чья",
    "чьё",
    "чье",
    "чьи",
    "сколько",
    "разве",
    "неужели",
    "отчего",
    "почём",
    "почем",
    "чего",
    "кому",
    "кого",
    "чему",
    "кем",
    "чем",
}

TAG_QUESTION_TAILS = (
    "правда",
    "серьёзно",
    "серьезно",
    "ты уверен",
    "ты уверена",
    "вы уверены",
    "понял",
    "поняла",
    "понятно",
    "да",
    "а",
    "не так ли",
    "не правда ли",
    "верно",
    "ведь так",
    "так ведь",
    "ты шутишь",
    "или нет",
    "или как",
    "или что",
    "что ли",
    "слышишь",
    "видишь",
    "знаешь",
    "ну как",
    "ну что",
)

COLLOQUIAL_QUESTION_SUBSTRINGS = (
    "что ли",
    "с ума сошёл",
    "с ума сошел",
    "с ума сошла",
    "ты спятил",
    "ты спятила",
    "ты рехнулся",
    "ты рехнулась",
    "ты издеваешься",
    "вы издеваетесь",
    "ты оглох",
    "ты ослеп",
)

EXCLAMATION_STARTS = (
    "эй",
    "стой",
    "стойте",
    "беги",
    "бегите",
    "быстрее",
    "быстро",
    "осторожно",
    "берегись",
    "помогите",
    "спасите",
    "заткнись",
    "замолчи",
    "чёрт",
    "черт",
    "вот чёрт",
    "вот черт",
    "к чёрту",
    "к черту",
    "господи",
    "о боже",
    "боже мой",
    "проклятье",
    "ура",
    "вау",
    "ложись",
    "пригнись",
    "в укрытие",
    "стреляй",
    "не стреляй",
    "огонь",
    "бросай оружие",
    "пошёл вон",
    "пошел вон",
    "вон отсюда",
    "прочь",
    "убирайся",
    "хватит",
    "брось",
    "отвали",
    "погнали",
    "вперёд",
    "вперед",
    "назад",
    "за мной",
    "гони",
    "газуй",
    "жми",
    "тормози",
    "держись",
    "держи его",
    "хватайте",
    "не двигайся",
    "ни с места",
    "руки вверх",
    "руки за голову",
    "всем лежать",
    "на пол",
    "нет",
    "давай",
)

CLAUSE_SPLIT_RE = re.compile(r"[,;:\-–—]+")
WORD_RE = re.compile(r"[а-яёa-z0-9\-]+", re.IGNORECASE)
PARTICLE_LI_RE = re.compile(r"^\s*[а-яё]+\s+(?:[а-яё]+\s+)?ли\b", re.IGNORECASE)


def restore_punctuation_heuristic(text: str) -> str:
    """Восстанавливает недостающие конечные знаки препинания (., ?, !) менее чем за 1 мс, не дублируя уже имеющиеся."""
    s = text.strip()
    if not s:
        return s

    # 1. Быстрый выход (0 мс): уже есть завершающий знак препинания (включая ... или …)
    #    или знак продолжения (запятая/тире/двоеточие, когда игра делит фразу на 2 субтитра)
    stripped_quotes = s.rstrip('»"\' ')
    if stripped_quotes.endswith(TERMINAL_PUNCT) or stripped_quotes.endswith(CONTINUATION_PUNCT):
        return s

    lower = stripped_quotes.lower()

    # 2. Эвристики вопроса (?)
    clauses = [c.strip() for c in CLAUSE_SPLIT_RE.split(lower) if c.strip()]
    first_words = WORD_RE.findall(clauses[0]) if clauses else []

    is_question = False
    if first_words and first_words[0] in QUESTION_WORDS:
        # Защита от утвердительных оборотов «что ж» / «как же» / «как будто»
        if not (
            (first_words[0] == "что" and len(first_words) > 1 and first_words[1] in ("ж", "же"))
            or (first_words[0] == "как" and len(first_words) > 1 and first_words[1] in ("будто", "раз", "только"))
        ):
            is_question = True

    if not is_question and any(sub in lower for sub in COLLOQUIAL_QUESTION_SUBSTRINGS):
        is_question = True

    if not is_question and clauses:
        # Вопросительное слово после запятой/тире в следующей части (например: «Слушай, куда ты идёшь»)
        for clause in clauses[1:]:
            cw = WORD_RE.findall(clause)
            if cw and cw[0] in QUESTION_WORDS:
                if not (
                    cw[0] == "что"
                    and len(cw) > 1
                    and cw[1]
                    not in (
                        "ты",
                        "вы",
                        "он",
                        "она",
                        "это",
                        "тут",
                        "здесь",
                        "там",
                        "случилось",
                        "происходит",
                        "такое",
                        "дальше",
                        "делать",
                    )
                ):
                    is_question = True
                    break
            if PARTICLE_LI_RE.match(clause):
                is_question = True
                break

    if not is_question and PARTICLE_LI_RE.match(lower):
        is_question = True

    if not is_question and len(clauses) >= 2:
        last_clause = clauses[-1].strip()
        if last_clause in TAG_QUESTION_TAILS:
            is_question = True
    elif not is_question and len(clauses) == 1:
        if lower in (
            "правда",
            "серьёзно",
            "серьезно",
            "ты уверен",
            "ты уверена",
            "вы уверены",
            "ты шутишь",
            "понял",
            "поняла",
            "ну как",
            "ну что",
        ):
            is_question = True

    if is_question:
        return s + "?"

    # 3. Эвристики восклицания (!)
    all_words = WORD_RE.findall(lower)
    is_exclamation = False
    for phrase in EXCLAMATION_STARTS:
        if (
            lower == phrase
            or lower.startswith(phrase + " ")
            or lower.startswith(phrase + ",")
            or lower.startswith(phrase + " —")
            or lower.startswith(phrase + "-")
        ):
            if phrase in ("нет", "давай") and len(all_words) > 6:
                continue
            is_exclamation = True
            break

    if not is_exclamation and len(clauses) >= 2:
        for clause in clauses[1:]:
            for phrase in EXCLAMATION_STARTS:
                if clause == phrase or clause.startswith(phrase + " "):
                    if phrase not in ("нет", "давай") or len(all_words) <= 6:
                        is_exclamation = True
                        break
            if is_exclamation:
                break

    if is_exclamation:
        return s + "!"

    # 4. Гарантированная точка в конце (.)
    return s + "."


class PunctuationRestorer:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def restore(self, text: str) -> str:
        if not self.enabled:
            return text.strip()
        s = text.strip()
        if not s:
            return s
        stripped = s.rstrip('»"\' ')
        if stripped.endswith(TERMINAL_PUNCT) or stripped.endswith(CONTINUATION_PUNCT):
            return s
        return restore_punctuation_heuristic(s)
