import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DECK = "EnglishAuto"
NOTE_TYPE = "Простая (с вводом ответа)"
MODELS = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-flash-lite-latest"]
ANKI_URL = "http://localhost:8765"
KEY_NAME = "GEMINI_API_KEY"

HERE = Path(__file__).parent
WORDS = HERE / "words.txt"
DONE = HERE / "words_done.txt"
GAP = "______"

PROMPT = """Ты составляешь карточку Anki для русскоязычного ученика, который учит английский.
Слово или фраза: "{word}"
{sentence_rule}
Это может быть отдельное слово, фразовый глагол, устойчивое выражение или идиома. Если это фраза, работай с ней как с единым целым: пропуск в gap_sentence заменяет всю фразу целиком, коллокации и синонимы подбирай к фразе, а в reading_ru и ipa дай чтение всей фразы.

Верни JSON со следующими полями:
- problem: пустая строка, если всё в порядке. Если такого слова или фразы в английском нет (опечатка, выдумано) или у слова нет значения, которое просит ученик, — коротко по-русски объясни, в чём проблема, и назови настоящие значения или правильное написание. Не подгоняй карточку под несуществующее значение.
- word: слово ровно в той форме, в какой оно дано
- headline_ru: 1–2 кратких русских перевода
- front_ru: 2–3 русских перевода через запятую/точку с запятой (подсказка на лицевой стороне)
- gap_sentence: естественное английское предложение, где вместо слова стоит {gap} (ровно один пропуск, само слово в предложении не встречается)
- reading_ru: как слово читается русскими буквами, с ударением (знак ́ после ударной гласной), например "уа́йдэн"
- ipa: транскрипция IPA в косых чертах, например "/ˈwaɪdən/"
- pos: часть речи на английском: verb / noun / adjective / adverb / phrasal verb / phrase / idiom; для формы слова уточни в скобках, например "verb (past tense / past participle)"
- explanation: определение на простом английском, 1–2 предложения
- translation_ru: русские переводы основных значений
- forms: список строк. verb и phrasal verb: ["V1: ...", "V2: ...", "V3: ...", "-ing: ..."]; adjective: ["comparative: ...", "superlative: ..."]; noun: ["singular: ...", "plural: ..."] или ["uncountable"]; phrase / idiom: 1–3 строки с вариантами и схемой употребления, например ["give the impression that + clause", "give the impression of + noun / -ing"]; adverb — пустой список
- collocations: до 6 коллокаций, каждая {{"en": ..., "ru": ...}}. Только те, что реально и часто встречаются у носителей (как в словарях Oxford, Cambridge, Longman). Ничего не выдумывай и не собирай механически из слова и случайных соседей. Не путай с похожими словами (не "conversely proportional", а "inversely proportional"). Каждая коллокация должна содержать само слово или фразу; синонимы и близкие по смыслу выражения сюда не клади. Если уверенных коллокаций меньше шести, дай меньше; если их нет, верни пустой список
- synonyms: до 3 синонимов той же части речи, что и слово, каждый {{"en": ..., "ru": ...}}; только реально употребительные
- examples: 3 примера, каждый {{"en": ..., "ru": ...}}; первый пример — это gap_sentence с вписанным словом
- word_family: до 5 однокоренных слов, каждое {{"en": ..., "ru": ...}}; только употребительные слова с тем же корнем, без редких и выдуманных. Если таких нет, верни пустой список
- register: например "neutral", "formal", "informal", "neutral / formal"
- level: уровень CEFR, например "B1"
"""

PAIRS = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"en": {"type": "STRING"}, "ru": {"type": "STRING"}},
        "required": ["en", "ru"],
    },
}
STRING_FIELDS = [
    "problem", "word", "headline_ru", "front_ru", "gap_sentence", "reading_ru", "ipa",
    "pos", "explanation", "translation_ru", "register", "level",
]
LIST_FIELDS = ["forms", "collocations", "synonyms", "examples", "word_family"]
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **{f: {"type": "STRING"} for f in STRING_FIELDS},
        "forms": {"type": "ARRAY", "items": {"type": "STRING"}},
        "collocations": PAIRS,
        "synonyms": PAIRS,
        "examples": PAIRS,
        "word_family": PAIRS,
    },
    "required": STRING_FIELDS + LIST_FIELDS,
}


def api_key():
    key = os.environ.get(KEY_NAME)
    if not key and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, KEY_NAME)[0]
        except OSError:
            pass
    if not key:
        sys.exit(f"Нет ключа, нужна переменная {KEY_NAME}")
    return key


def post(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def anki(action, **params):
    try:
        reply = post(ANKI_URL, {"action": action, "version": 6, "params": params})
    except urllib.error.URLError:
        sys.exit("Anki не отвечает, откройте Anki с AnkiConnect")
    if reply["error"]:
        raise RuntimeError(reply["error"])
    return reply["result"]


def generate(key, word, sentence, hint):
    if sentence:
        rule = (
            f'Ученик встретил слово в предложении: "{sentence}". Для gap_sentence используй именно его, '
            "заменив слово на пропуск. Переводы, explanation и коллокации давай для того значения, "
            "в котором слово употреблено в этом предложении."
        )
    else:
        rule = "Предложение для gap_sentence придумай сам."
    if hint:
        rule += (
            f' Ученику нужно это слово именно в значении "{hint}". Вся карточка — переводы, explanation, '
            "предложения, коллокации, синонимы — должна быть про это значение, а не про самое частое."
        )
    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(word=word, sentence_rule=rule, gap=GAP)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.4,
        },
    }
    error = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                reply = post(url, payload, {"x-goog-api-key": key})
                return json.loads(reply["candidates"][0]["content"]["parts"][0]["text"])
            except urllib.error.HTTPError as e:
                error = f"Gemini {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
                if e.code not in (429, 500, 503):
                    raise RuntimeError(error)
                print(f"  {model}: {e.code}, пробую ещё")
                if attempt == 0:
                    time.sleep(15)
    raise RuntimeError(error)


def pairs(items, bullet="• "):
    return "<br>".join(f"{bullet}{p['en']} — {p['ru']}" for p in items)


def build(c):
    if c["problem"].strip():
        raise RuntimeError(c["problem"].strip())
    if GAP not in c["gap_sentence"]:
        raise RuntimeError("в предложении нет пропуска")
    front = f"{c['front_ru']}<br><br>{c['gap_sentence']}"
    examples = "<br><br>".join(
        f"{i}. {e['en']}<br>— {e['ru']}" for i, e in enumerate(c["examples"], 1)
    )
    blocks = [
        f"{c['word']} — {c['headline_ru']}",
        f"Как читается русскими буквами: {c['reading_ru']}<br>Транскрипция: {c['ipa']}",
        f"Часть речи: {c['pos']}",
        f"Explanation:<br>{c['explanation']}",
        f"Перевод:<br>{c['translation_ru']}",
    ]
    if c["forms"]:
        blocks.append("Forms:<br>" + "<br>".join(c["forms"]))
    if c["collocations"]:
        blocks.append("Collocations:<br>" + pairs(c["collocations"]))
    if c["synonyms"]:
        blocks.append("Synonyms:<br>" + pairs(c["synonyms"]))
    blocks.append("Example sentences:<br>" + examples)
    if c["word_family"]:
        blocks.append("Word family:<br>" + pairs(c["word_family"], bullet=""))
    blocks += [f"Register: {c['register']}", f"Level: {c['level']}"]
    return front, "<br><br>".join(blocks)


def exists(word):
    return bool(anki("findNotes", query=f'"note:{NOTE_TYPE}" "Back:{word} — *"'))


def mark_done(line):
    with DONE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry-run" in sys.argv
    if not WORDS.exists():
        sys.exit("Нет words.txt")
    lines = [l.strip() for l in WORDS.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        sys.exit("words.txt пуст")

    key = api_key()
    anki("version")
    left = []
    for line in lines:
        word, _, sentence = (p.strip() for p in line.partition("|"))
        hint = ""
        if m := re.fullmatch(r"(.+?)\s*\((.+)\)", word):
            word, hint = m.groups()
        print(f"{word}:" + (f" [{hint}]" if hint else ""))
        if not hint and exists(word):
            print("  уже есть в колоде")
            if not dry:
                mark_done(line)
            continue
        try:
            front, back = build(generate(key, word, sentence, hint))
            if dry:
                print("  FRONT:\n    " + front.replace("<br>", "\n    "))
                print("  BACK:\n    " + back.replace("<br>", "\n    "))
                left.append(line)
                continue
            anki("addNote", note={
                "deckName": DECK,
                "modelName": NOTE_TYPE,
                "fields": {"Front": front, "Back": back},
                "options": {"allowDuplicate": True},
            })
            mark_done(line)
            print("  добавлено")
        except Exception as e:
            print(f"  ошибка: {e}")
            left.append(line)

    if not dry:
        WORDS.write_text("".join(l + "\n" for l in left), encoding="utf-8")
        print(f"\nне обработано: {len(left)}")


if __name__ == "__main__":
    main()
