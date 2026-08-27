"""Fetch a fact / quote / word-of-the-day / advice snippet from a free public API
and append it to KNOWLEDGE_LOG.md. Runs unattended from GitHub Actions, so any
single API being down or slow must never crash the run -- it should just try the
next source and, if all of them fail, log a friendly note instead of an entry.
"""

import datetime
import json
import random
import urllib.error
import urllib.request
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "KNOWLEDGE_LOG.md"
TIMEOUT = 10
USER_AGENT = "jarvis-knowledge-log-bot/1.0 (+github-actions)"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_quote() -> str:
    data = _get_json("https://api.quotable.io/random")
    return f"**Quote of the day:** \"{data['content']}\" — {data['author']}"


def fetch_advice() -> str:
    data = _get_json("https://api.adviceslip.com/advice")
    return f"**Advice of the day:** {data['slip']['advice']}"


def fetch_fact() -> str:
    data = _get_json("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
    return f"**Fact of the day:** {data['text']}"


def fetch_word_of_the_day() -> str:
    word_data = _get_json("https://random-word-api.herokuapp.com/word")
    word = word_data[0]
    entries = _get_json(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}")
    meaning = entries[0]["meanings"][0]
    part_of_speech = meaning.get("partOfSpeech", "")
    definition = meaning["definitions"][0]["definition"]
    return f"**Word of the day:** *{word}* ({part_of_speech}) — {definition}"


# Tried in random order each run so the log doesn't fall into a fixed pattern;
# whichever source responds first wins for that run.
SOURCES = [fetch_quote, fetch_advice, fetch_fact, fetch_word_of_the_day]


def build_entry() -> str:
    today = datetime.date.today().isoformat()
    sources = SOURCES[:]
    random.shuffle(sources)
    for source in sources:
        try:
            return f"## {today}\n\n{source()}\n"
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            print(f"[knowledge_log] {source.__name__} failed: {exc}")
            continue
    return f"## {today}\n\n_No source was reachable today — all public APIs failed or timed out._\n"


def main() -> None:
    entry = build_entry()

    if not LOG_PATH.exists():
        LOG_PATH.write_text("# Knowledge Log\n\nA running log of daily facts, quotes, and words, "
                             "auto-posted by GitHub Actions.\n\n", encoding="utf-8")

    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write("\n" + entry)

    print(f"[knowledge_log] appended entry:\n{entry}")


if __name__ == "__main__":
    main()
