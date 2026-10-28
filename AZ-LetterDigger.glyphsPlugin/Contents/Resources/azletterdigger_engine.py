"""AZ Letter Digger scoring engine.

Pure-stdlib port of Get_Best_Words v2: ranks corpus words by typographic
usefulness for testing a given letter (letter-group diversity, per-letter
difficulty ratings, diacritic density, letter-pair texture).

No Glyphs imports here: this module is unit-tested outside Glyphs.
"""

from __future__ import annotations

import functools
import gzip
import itertools
import json
import random
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

WORD_LENGTH_SCORE = 15
GROUP_PREFERENCE_BONUS = 20
GROUP_PREFERENCE_PENALTY = 15
DIVERSITY_BONUS = 4
LETTER_FREQUENCY_BONUS = 10
MARK_FREQUENCY_BONUS = 50
TEXTURE_BONUS = 6
DOTTED_I_LANGUAGES = frozenset({"Turkish"})  # i/İ and ı/I are case pairs


class Case(str, Enum):  # noqa: UP042 — str+Enum keeps Glyphs Python 3.10 working
    AUTO = "auto"  # each typed letter's own case
    CAPITALIZE = "capitalize"


class Position(str, Enum):  # noqa: UP042
    START = "start"
    CONTAIN = "contain"


@dataclass(frozen=True)
class ScoreWeights:
    """User-tunable scoring sliders, all neutral at 0."""

    diversity: float = 0.0
    letter_frequency: float = 0.0
    mark_frequency: float = 0.0
    texture: float = 0.0  # >0 rewards common letter bigrams, <0 rewards rare ones


@dataclass(frozen=True)
class SearchConfig:
    letters: tuple[str, ...] = ()  # one bucket of words per letter
    all_letters: bool = False  # several letters: ONE bucket of words holding them all
    most_letters: bool = False  # with all_letters: holding as many of them as possible
    languages: tuple[str, ...] = ()  # empty means all available languages
    results_per_letter: int = 10
    min_length: int = 3
    max_length: int = 11
    positions: tuple[Position, ...] = (Position.CONTAIN,)
    case: Case = Case.AUTO
    preferred_groups: tuple[str, ...] = ()
    preferred_chars: frozenset[str] = frozenset()  # folded; font-derived feature set
    unique_letters: bool = False
    charset: frozenset[str] | None = None  # the font's characters; None = no font, no limit
    smart: bool = True  # False: random words fitting the rules, unscored (word-o-mat style)
    words_per_language: int = 2000  # per-language cap on words holding the letters
    weights: ScoreWeights = field(default_factory=ScoreWeights)


@dataclass(frozen=True)
class LetterWords:
    """One result bucket: best words for one letter at one position."""

    letter: str | None  # None for the "any good words" (no letter) search
    position: Position
    words: tuple[str, ...]


@dataclass(frozen=True)
class ScriptTable:
    groups: dict[str, frozenset[str]]  # raw characters, as authored
    rating: dict[int, frozenset[str]]  # folded characters
    groups_folded: dict[str, frozenset[str]]  # folded characters


EMPTY_TABLE = ScriptTable(groups={}, rating={}, groups_folded={})


@functools.lru_cache(maxsize=131072)
def fold_char(char: str) -> str:
    """Base character, casefolded: "É" -> "e", "é" -> "e", "ж" -> "ж"."""
    return unicodedata.normalize("NFKD", char)[0].casefold()[0]


@functools.lru_cache(maxsize=131072)
def fold_word(word: str) -> tuple[str, ...]:
    return tuple(fold_char(char) for char in word)


def _fold_set(chars: Iterable[str]) -> frozenset[str]:
    return frozenset(fold_char(char) for char in chars)


class RatingsDatabase:
    """Letter-group and difficulty-rating tables, keyed by script and case.

    Rating and diversity-group characters are folded at load time so scoring
    is one set-membership test per character, valid for any script and case.
    """

    def __init__(self, raw: dict[str, dict[str, dict[str, dict[str, list[str]]]]]) -> None:
        self._tables: dict[tuple[str, str], ScriptTable] = {}
        for script, cases in raw.items():
            for case, table in cases.items():
                groups = {name: frozenset(chars) for name, chars in table.get("groups", {}).items()}
                self._tables[(script, case)] = ScriptTable(
                    groups=groups,
                    rating={
                        int(value): _fold_set(chars)
                        for value, chars in table.get("rating", {}).items()
                    },
                    groups_folded={name: _fold_set(chars) for name, chars in groups.items()},
                )

    @classmethod
    def from_file(cls, path: Path) -> RatingsDatabase:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def table(self, script: str, case: str) -> ScriptTable:
        return self._tables.get((script, case), EMPTY_TABLE)

    def group_names(self, script: str = "Latin", case: str = "lowercase") -> tuple[str, ...]:
        return tuple(self.table(script, case).groups)

    def group_chars(self, name: str) -> frozenset[str]:
        """Raw characters of a named group, unioned across every script/case."""
        chars: set[str] = set()
        for table in self._tables.values():
            chars |= table.groups.get(name, frozenset())
        return frozenset(chars)


class WordSource:
    """Lazy, cached access to the bundled per-language wordlists.

    Lists are stored one word per line, most frequent first; line order is the
    frequency ranking.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._wordlist_dir = data_dir / "wordlists"
        manifest_path = data_dir / "languages.json"
        self._manifest: dict[str, str] = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        )
        self._external: dict[str, Path] = {}  # name -> a plain-text list outside the bundle
        self._cache: dict[str, tuple[str, ...]] = {}
        self._charset_cache: dict[tuple[str, int], frozenset[str]] = {}
        self._orthographies: dict[str, frozenset[str]] | None = None  # lazy, vendored
        self._bigram_cache: dict[tuple, frozenset[str]] = {}

    def orthography_chars(self, language: str) -> frozenset[str] | None:
        """Authoritative, case-folded character set for a bundled language from
        the vendored hyperglot orthographies (base + auxiliary of every
        orthography). None when the language has no entry, so callers fall
        back to the frequency-capped corpus charset. Loaded once."""
        if self._orthographies is None:
            path = self._data_dir / "orthographies.json"
            try:
                raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError):
                raw = {}
            self._orthographies = {
                name: frozenset(char.casefold() for char in chars) for name, chars in raw.items()
            }
        return self._orthographies.get(language)

    @property
    def languages(self) -> tuple[str, ...]:
        """The bundled languages, then the outside lists in the order added."""
        return tuple(sorted(self._manifest)) + tuple(self._external)

    def add_wordlist(self, name: str, path: Path) -> None:
        """Offer a plain-text list (one word per line) beside the bundled ones."""
        self.remove_wordlist(name)
        self._external[name] = path

    def remove_wordlist(self, name: str) -> None:
        self._external.pop(name, None)
        self._cache.pop(name, None)
        self._charset_cache = {
            key: value for key, value in self._charset_cache.items() if key[0] != name
        }
        _POOL_CACHE.clear()

    def words(self, language: str) -> tuple[str, ...]:
        if language in self._cache:
            return self._cache[language]
        external = self._external.get(language)
        filename = self._manifest.get(language)
        path = external or (self._wordlist_dir / filename if filename else None)
        if path is None or not path.exists():
            return ()
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                # NFC at load: decomposed corpus entries would never match the
                # (composed) search target and hide their marks from count_marks.
                loaded = tuple(
                    unicodedata.normalize("NFC", stripped)
                    for line in handle
                    if (stripped := line.strip())
                )
        except OSError:
            loaded = ()
        if external:
            loaded = _clean_wordlist(loaded)
        self._cache[language] = loaded
        return loaded

    def charset_for(self, language: str, top_n: int = 300) -> frozenset[str]:
        """Characters used by the language's top_n words — its essential
        alphabet. Reads only the head of the gzipped list (lists are ordered
        by frequency), so probing all languages stays cheap."""
        cached = self._charset_cache.get((language, top_n))
        if cached is not None:
            return cached
        words: tuple[str, ...]
        if language in self._cache or language in self._external:
            # An outside list is cleaned (and maybe shuffled) as a whole first.
            words = self.words(language)[:top_n]
        else:
            filename = self._manifest.get(language)
            path = self._wordlist_dir / filename if filename else None
            if path is None or not path.exists():
                return frozenset()
            head: list[str] = []
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped:
                        head.append(unicodedata.normalize("NFC", stripped))
                    if len(head) >= top_n:
                        break
            words = tuple(head)
        charset = frozenset(char for word in words for char in word)
        self._charset_cache[(language, top_n)] = charset
        return charset

    def common_bigrams(
        self, languages: Sequence[str], top_words: int = 2000, top_k: int = 150
    ) -> frozenset[str]:
        """The languages' most frequent adjacent letter pairs — their "texture".

        Computed lazily from the wordlist heads (no build step): each language
        contributes its top_k bigrams by occurrence count over its top_words
        most frequent words.
        """
        key = (tuple(languages), top_words, top_k)
        cached = self._bigram_cache.get(key)
        if cached is not None:
            return cached
        combined: set[str] = set()
        for language in languages:
            counts = Counter(
                word[i : i + 2]
                for word in self.words(language)[:top_words]
                for i in range(len(word) - 1)
            )
            combined.update(pair for pair, _ in counts.most_common(top_k))
        result = frozenset(combined)
        if len(self._bigram_cache) >= 32:
            self._bigram_cache.clear()
        self._bigram_cache[key] = result
        return result

    def dominant_script(self, languages: Sequence[str]) -> str:
        """Most common script across the languages' essential alphabets, one
        vote per language; "Latin" when nothing is known."""
        votes: Counter[str] = Counter()
        for language in languages:
            scripts = Counter(
                script_of(char) for char in self.charset_for(language) if char.isalpha()
            )
            if scripts:
                votes[scripts.most_common(1)[0][0]] += 1
        return votes.most_common(1)[0][0] if votes else "Latin"


def script_of(char: str) -> str:
    """Best-effort script name ("Latin", "Cyrillic", …) from the Unicode name."""
    try:
        return unicodedata.name(char).split(" ")[0].capitalize()
    except ValueError:
        return "Unknown"


def case_of(char: str) -> str:
    # caseless scripts score against the lowercase table
    return "uppercase" if char.isupper() else "lowercase"


def is_word(token: str) -> bool:
    """Letters plus combining marks (and ZWJ) only, at least one letter.

    str.isalpha() rejects every Indic word carrying vowel signs (Mn/Mc
    marks), which gutted the Malayalam/Tamil/Telugu/Sinhala wordlists.
    """
    has_letter = False
    for char in token:
        if char == "‍":  # zero-width joiner inside Indic clusters
            continue
        category = unicodedata.category(char)[0]
        if category == "L":
            has_letter = True
        elif category != "M":
            return False
    return has_letter


def _clean_wordlist(words: Sequence[str]) -> tuple[str, ...]:
    """An outside list in corpus shape: lowercase (the Case options re-case it), words
    only, each once. Kept in file order, so a frequency list stays ranked; a mostly
    alphabetical one (the macOS dictionary) is shuffled, since the per-language cap
    would otherwise never get past the start of the alphabet."""
    kept = list(dict.fromkeys(word.lower() for word in words if is_word(word)))
    if sum(first <= second for first, second in itertools.pairwise(kept)) > 0.9 * len(kept):
        random.Random(0).shuffle(kept)
    return tuple(kept)


def search_target(letter: str, dotted_i: bool = False) -> str:
    """Single lowercase character to look for in (lowercase) corpus words.

    Handles one-to-many case mappings: "İ".lower() is "i" plus a combining
    dot (two code points, never present in the NFC corpus), so combining
    marks are stripped before taking the base character. dotted_i: Turkish
    casing, where I is the capital of dotless ı.
    """
    if dotted_i and letter == "I":
        return "ı"
    lowered = letter.lower()
    if len(lowered) > 1:
        stripped = "".join(
            char
            for char in unicodedata.normalize("NFD", lowered)
            if not unicodedata.combining(char)
        )
        lowered = stripped or lowered
    return lowered[0]


def count_marks(text: str) -> int:
    """Number of combining marks, literal or hidden in precomposed characters."""
    marks = 0
    for char in text:
        if unicodedata.category(char) in {"Mn", "Mc"}:
            marks += 1
            continue
        decomposition = unicodedata.decomposition(char)
        if not decomposition:
            continue
        for code_point in decomposition.split():
            if code_point.startswith("<"):
                continue
            try:
                component = chr(int(code_point, 16))
            except ValueError:
                continue
            if unicodedata.category(component) in {"Mn", "Mc"}:
                marks += 1
    return marks


def count_pairs(pairs: Collection[str], text: str) -> int:
    """How many distinct pairs occur in text (pairs are 2-character strings)."""
    return len({text[i : i + 2] for i in range(len(text) - 1)}.intersection(pairs))


def apply_case(word: str, case: str, dotted_i: bool = False) -> str:
    """Re-case a (lowercase corpus) word for output.

    dotted_i: Turkish casing (a Turkish word, or the searched letter is İ) —
    str.upper() maps i to plain I, so the i's must become İ first ("ikinci"
    -> "İKİNCİ", not "IKINCI"). Same family of problem as ß: upper-casing
    to "SS" changes the word length and loses the letter, so the capital
    sharp s is used.
    """
    if case == "uppercase":
        word = word.replace("ß", "ẞ")
        if dotted_i:
            word = word.replace("i", "İ")
        return word.upper()
    if case == "capitalize":
        if dotted_i and word[:1] == "i":
            return "İ" + word[1:].lower()
        return word.capitalize()
    return word


def _score_word(
    word: str,
    table: ScriptTable,
    diversity: float,
    preferred_groups: tuple[str, ...],
    preferred_chars: frozenset[str] = frozenset(),
) -> int:
    score = len(word) * WORD_LENGTH_SCORE
    folded = fold_word(word)

    for value, chars in table.rating.items():
        score += value * sum(char in chars for char in folded)

    if diversity:
        # distinct construction groups touched: reward mixing, not repetition
        touched = sum(
            1
            for group_chars in table.groups_folded.values()
            if any(char in group_chars for char in folded)
        )
        score += int(DIVERSITY_BONUS * diversity * touched)

    # A group this table doesn't define has to be a no-op: an empty set can
    # never match, so every character would take the penalty (-15 each, exactly
    # cancelling WORD_LENGTH_SCORE) instead of being ignored.
    preference_sets = [
        chars for group in preferred_groups if (chars := table.groups_folded.get(group))
    ]
    if preferred_chars:
        preference_sets.append(preferred_chars)
    for group_chars in preference_sets:
        seen: set[str] = set()
        for char in folded:
            if char in group_chars and char not in seen:
                score += GROUP_PREFERENCE_BONUS
                seen.add(char)
            else:
                score -= GROUP_PREFERENCE_PENALTY

    return score


# Candidate pools are deterministic for a given source state and filter set;
# cache them so repeated runs only pay for scoring.
_POOL_CACHE: dict[tuple, dict[str, bool]] = {}
_POOL_CACHE_LIMIT = 128


def _candidate_pool(
    config: SearchConfig,
    source: WordSource,
    languages: Sequence[str],
    letters: Sequence[str],
    start: bool = False,
    case: str = "lowercase",
    most: bool = False,
) -> dict[str, bool]:
    """Frequency-ordered, deduplicated pool of words holding every one of
    ``letters`` (``start``: beginning with the first one; ``case`` "capitalize":
    still showing each letter as typed once Capitalized, so "b" can't be only the
    initial), capped per language AFTER those filters. ``most``: words holding
    at least one, and the cap keeps those holding the most. Maps each word to
    its Turkish-casing flag (apply_case's dotted_i), from the first language
    listing it. With a font charset, only words it can set in the output ``case``."""
    capitalize = case == "capitalize"
    key = (
        id(source),
        tuple(languages),
        tuple(letters),
        start,
        case,
        most,
        config.min_length,
        config.max_length,
        config.unique_letters,
        config.words_per_language,
        config.charset,
    )
    pool = _POOL_CACHE.get(key)
    if pool is None:
        # No per-word alpha check here: bundled lists are filtered at build
        # time (scripts/build_data.py).
        def language_words(language: str, dotted_i: bool) -> Iterable[str]:
            targets = tuple(search_target(letter, dotted_i) for letter in letters)
            fitting = (
                word
                for word in source.words(language)
                if config.min_length <= len(word) <= config.max_length
                and (not start or word.startswith(targets[0]))
                and (not config.unique_letters or len(set(word)) == len(word))
            )
            if config.charset is not None:
                charset = config.charset
                fitting = (
                    word for word in fitting if charset.issuperset(apply_case(word, case, dotted_i))
                )
            if not most:
                every = (
                    word
                    for word in fitting
                    if _letters_shown(word, letters, targets, capitalize, dotted_i) == len(letters)
                )
                return itertools.islice(every, config.words_per_language)
            # Rank the whole list by letters held, or the cap would keep the
            # most frequent words holding one and starve the rare ones holding
            # them all. The sort is stable: frequency order within a count.
            shown = {
                word: count
                for word in fitting
                if (count := _letters_shown(word, letters, targets, capitalize, dotted_i))
            }
            return sorted(shown, key=shown.__getitem__, reverse=True)[: config.words_per_language]

        pool = {}
        for language in languages:
            dotted_i = language in DOTTED_I_LANGUAGES or "İ" in letters
            for word in language_words(language, dotted_i):
                pool.setdefault(word, dotted_i)
        if len(_POOL_CACHE) >= _POOL_CACHE_LIMIT:
            _POOL_CACHE.clear()
        _POOL_CACHE[key] = pool

    return pool


def _letters_shown(
    word: str, letters: Sequence[str], targets: Sequence[str], capitalize: bool, dotted_i: bool
) -> int:
    """How many of ``letters`` the word shows: as typed once Capitalized, else
    by their (language-aware) search targets."""
    if capitalize:
        cased = apply_case(word, "capitalize", dotted_i)
        return sum(letter in cased for letter in letters)
    return sum(target in word for target in targets)


def _targets_by_casing(letters: Sequence[str]) -> dict[bool, tuple[str, ...]]:
    """The letters' search targets, keyed by the Turkish-casing flag."""
    return {
        dotted_i: tuple(search_target(letter, dotted_i) for letter in letters)
        for dotted_i in (False, True)
    }


def _ranked_words(
    pool: Mapping[str, bool],
    letters: Sequence[str],
    table: ScriptTable,
    config: SearchConfig,
    rng: random.Random,
    common_bigrams: frozenset[str] = frozenset(),
) -> list[str]:
    if not config.smart:
        fitting = list(pool)
        rng.shuffle(fitting)
        return fitting
    weights = config.weights
    targets = _targets_by_casing(letters)
    letter_has_marks = any(count_marks(letter) for letter in letters)
    scores: dict[str, float] = {}

    for word, dotted_i in pool.items():
        score = _score_word(
            word, table, weights.diversity, config.preferred_groups, config.preferred_chars
        )

        if weights.letter_frequency:
            # every copy of a letter that appears more than once
            repeats = sum(
                count for target in targets[dotted_i] if (count := word.count(target)) > 1
            )
            if repeats:
                score += int(LETTER_FREQUENCY_BONUS * repeats * weights.letter_frequency)

        if letter_has_marks and weights.mark_frequency:
            marks = count_marks(word)
            if marks:
                score += int(MARK_FREQUENCY_BONUS * marks * weights.mark_frequency)

        if weights.texture:
            texture_pairs = count_pairs(common_bigrams, word)
            if texture_pairs:
                score += int(TEXTURE_BONUS * texture_pairs * weights.texture)

        scores[word] = score

    return sorted(scores, key=lambda word: scores[word], reverse=True)


def _select(
    ranked: Iterable[str],
    pool: Mapping[str, bool],
    config: SearchConfig,
    effective_case: str,
) -> tuple[str, ...]:
    words: list[str] = []
    for word in ranked:
        if len(words) >= config.results_per_letter:
            break
        cased = apply_case(word, effective_case, pool[word])
        # Re-check length on the cased word: rare transforms expand it
        # (compatibility ligatures like ﬁ upper-case to two characters).
        # Skip re-cased twins too (Greek "της" and "τησ" both give "ΤΗΣ").
        if config.min_length <= len(cased) <= config.max_length and cased not in words:
            words.append(cased)
    return tuple(words)


def find_words(
    config: SearchConfig,
    source: WordSource,
    ratings: RatingsDatabase,
    rng: random.Random | None = None,
) -> list[LetterWords]:
    """Rank corpus words and return the best buckets per letter and position.

    With no letters, returns a single CONTAIN bucket of generally good test
    words scored against the lowercase table of the selected languages'
    dominant script.
    """
    rng = rng or random.Random()
    languages = tuple(config.languages) or source.languages
    common_bigrams = source.common_bigrams(languages) if config.weights.texture else frozenset()

    if not config.letters:
        output_case = config.case.value if config.case is not Case.AUTO else "lowercase"
        pool = _candidate_pool(config, source, languages, (), case=output_case)
        table = ratings.table(source.dominant_script(languages), "lowercase")
        ranked = _ranked_words(pool, (), table, config, rng, common_bigrams)
        words = _select(ranked, pool, config, output_case)
        return [LetterWords(letter=None, position=Position.CONTAIN, words=words)]

    if config.all_letters and len(config.letters) > 1:
        # One CONTAIN bucket (a word can't start with several letters), rated
        # against the first letter's table. AUTO case keeps the letters' case
        # when they all agree, else Capitalizes: "H" + "a" gives "Hand".
        first = config.letters[0]
        cases = {case_of(letter) for letter in config.letters}
        table = ratings.table(script_of(first), case_of(first))
        if config.case is not Case.AUTO:
            output_case = config.case.value
        else:
            output_case = cases.pop() if len(cases) == 1 else "capitalize"
        capitalize = output_case == "capitalize"
        pool = _candidate_pool(
            config,
            source,
            languages,
            config.letters,
            case=output_case,
            most=config.most_letters,
        )
        ranked = _ranked_words(pool, config.letters, table, config, rng, common_bigrams)
        if config.most_letters:
            # Most letters first; the ranking still orders each count (stable sort).
            targets = _targets_by_casing(config.letters)
            ranked.sort(
                key=lambda word: _letters_shown(
                    word, config.letters, targets[pool[word]], capitalize, pool[word]
                ),
                reverse=True,
            )
        words = _select(ranked, pool, config, output_case)
        return [LetterWords(letter="".join(config.letters), position=Position.CONTAIN, words=words)]

    results: list[LetterWords] = []
    for letter in config.letters:
        letter_case = case_of(letter)
        table = ratings.table(script_of(letter), letter_case)
        effective_case = config.case.value if config.case is not Case.AUTO else letter_case

        for position in config.positions:
            # One pool per position: START filters before the per-language cap,
            # or the first capped words holding the letter starve it.
            pool = _candidate_pool(
                config,
                source,
                languages,
                (letter,),
                start=position is Position.START,
                case=effective_case,
            )
            ranked = _ranked_words(pool, (letter,), table, config, rng, common_bigrams)
            words = _select(ranked, pool, config, effective_case)
            results.append(LetterWords(letter=letter, position=position, words=words))

    return results


# --- Glyph tags (the Feature popup's Tag: entries) ---------------------------


def unicode_char(code: Any) -> str | None:
    """The character of a Glyphs unicode value ("00E9"), None for none or junk."""
    try:
        return chr(int(str(code), 16)) if code else None
    except (OverflowError, ValueError):
        return None


def _glyph_char(glyph: Any) -> str | None:
    """Letter character for an encoded glyph, None for anything else."""
    char = unicode_char(glyph.unicode)
    return char if char and unicodedata.category(char).startswith("L") else None


# Cluster (AZ Classifier) group tags: "CL: <title>" curated, "CL auto: <title>"
# snapshots; the other prefixes are older builds' spellings.
CLUSTR_TAG_PREFIXES = ("CL: ", "TC: ", "TypeCase: ", "Clustr: ")
CLUSTR_AUTO_TAG_PREFIXES = ("CL auto: ", "TC auto: ", "TypeCase auto: ", "Clustr auto: ")


def _glyph_tags(glyph: Any) -> list[str]:
    try:
        return [str(tag) for tag in (glyph.tags or [])]
    except Exception:
        return []


def tag_title(tag: str) -> str:
    """Display title for a glyph tag: Clustr's locked/auto prefixes (and
    their legacy spellings) are stripped, so "TC: Bowls" and
    "TC auto: Bowls" both read — and merge — as "Bowls". Other tags pass
    through unchanged."""
    for prefix in CLUSTR_AUTO_TAG_PREFIXES + CLUSTR_TAG_PREFIXES:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return tag


def font_tag_titles(font: Any) -> list[str]:
    """Sorted unique tag titles across the font's glyphs."""
    titles = {tag_title(tag) for glyph in font.glyphs for tag in _glyph_tags(glyph)}
    return sorted(title for title in titles if title)


def chars_for_tag(font: Any, title: str) -> list[str]:
    """Characters of encoded letter glyphs carrying a tag with this title,
    in font order. Clustr locked/auto variants of the same title merge."""
    chars: list[str] = []
    for glyph in font.glyphs:
        if not any(tag_title(tag) == title for tag in _glyph_tags(glyph)):
            continue
        char = _glyph_char(glyph)
        if char is not None and char not in chars:
            chars.append(char)
    return chars
