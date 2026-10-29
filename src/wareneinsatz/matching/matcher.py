"""Which ingredient is this invoice line? Fuzzy text matching, embeddings, or both.

    matcher = Matcher(ingredients, embedder=OllamaEmbedder())
    matcher.match("HAEHNCHENBRUST TK 4x2,5kg")   # Match(key='haehnchenbrust', method='hybrid', score=0.83)

Each ingredient is described by its name, its aliases and descriptions a person has confirmed
before (the matcher learns from every confirmation). A line is compared with all of them:

    fuzzy      letters: rapidfuzz WRatio on the cleaned texts (typos, abbreviations, word order)
    embedding  meaning: cosine similarity of sentence embeddings (synonyms, English, compounds)
    hybrid     the average of both scores, so each covers the other's blind spots

Below the threshold there is no suggestion: a person picks the ingredient. Above it the best
ingredient is suggested, and a person confirms it once per supplier product; after that the
memory (supplier + article number or description) matches it automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from rapidfuzz import fuzz

from wareneinsatz.matching.embeddings import Embedder, Vector, cosine
from wareneinsatz.matching.normalize import matching_text


class Method(StrEnum):
    MEMORY = "memory"  # confirmed before for this supplier: exact
    FUZZY = "fuzzy"
    EMBEDDING = "embedding"
    HYBRID = "hybrid"
    NONE = "none"  # nothing good enough: a person picks


# Chosen on the development half of the matching set (eval/README.md), reported on the test half.
# The embedding and hybrid values belong to the default model, qwen3-embedding:0.6b.
THRESHOLDS = {Method.FUZZY: 0.68, Method.EMBEDDING: 0.76, Method.HYBRID: 0.74}


@dataclass(frozen=True)
class IngredientInfo:
    key: str
    name: str
    base_unit: str  # g, ml or piece
    is_food: bool = True
    texts: tuple[str, ...] = ()  # name, aliases and confirmed descriptions

    def all_texts(self) -> tuple[str, ...]:
        return (self.name, *self.texts)


@dataclass(frozen=True)
class Candidate:
    key: str
    score: float


@dataclass(frozen=True)
class Match:
    key: str | None  # the suggested ingredient; None below the threshold
    method: Method
    score: float  # of the best candidate, 0..1
    candidates: tuple[Candidate, ...] = field(default=())  # best three, for the review screen


class Matcher:
    def __init__(
        self,
        ingredients: Sequence[IngredientInfo],
        *,
        embedder: Embedder | None = None,
        method: Method | str | None = None,
        threshold: float | None = None,
    ):
        self.ingredients = {ingredient.key: ingredient for ingredient in ingredients}
        self.embedder = embedder
        self.method = Method(method) if method else (Method.HYBRID if embedder else Method.FUZZY)
        if self.method in (Method.EMBEDDING, Method.HYBRID) and embedder is None:
            raise ValueError(f"{self.method} matching needs an embedder")
        self.threshold = THRESHOLDS[self.method] if threshold is None else threshold
        # (ingredient key, cleaned text) for every text that describes an ingredient
        self._texts = [
            (ingredient.key, cleaned)
            for ingredient in ingredients
            for text in ingredient.all_texts()
            if (cleaned := matching_text(text))
        ]
        self._vectors: list[Vector] | None = None

    def learn(self, key: str, description: str) -> None:
        """A person confirmed that `description` is ingredient `key`: use it for future lines too."""
        cleaned = matching_text(description)
        if key not in self.ingredients or not cleaned or (key, cleaned) in self._texts:
            return
        self._texts.append((key, cleaned))
        if self._vectors is not None and self.embedder is not None:
            self._vectors.extend(self.embedder.embed([cleaned]))

    def match(self, description: str) -> Match:
        return self.match_many([description])[0]

    def match_many(self, descriptions: Sequence[str]) -> list[Match]:
        """Several lines at once: embeddings are requested in one batch."""
        cleaned = [matching_text(description) for description in descriptions]
        embedded = self._embedding_scores(cleaned) if self.method is not Method.FUZZY else [{} for _ in cleaned]
        results = []
        for text, embedding in zip(cleaned, embedded, strict=True):
            fuzzy = self._fuzzy_scores(text) if self.method is not Method.EMBEDDING else {}
            if self.method is Method.FUZZY:
                scores = fuzzy
            elif self.method is Method.EMBEDDING:
                scores = embedding
            else:
                scores = {key: (fuzzy.get(key, 0.0) + embedding.get(key, 0.0)) / 2 for key in self.ingredients}
            ranked = sorted(scores.items(), key=lambda item: -item[1])[:3]
            candidates = tuple(Candidate(key, round(score, 4)) for key, score in ranked)
            best = candidates[0] if candidates else None
            if best is None or best.score < self.threshold:
                results.append(Match(None, Method.NONE, best.score if best else 0.0, candidates))
            else:
                results.append(Match(best.key, self.method, best.score, candidates))
        return results

    def _fuzzy_scores(self, text: str) -> dict[str, float]:
        """Best WRatio per ingredient over its texts. WRatio handles partial matches and word order."""
        scores: dict[str, float] = {}
        if not text:
            return scores
        for key, candidate in self._texts:
            score = fuzz.WRatio(text, candidate) / 100
            if score > scores.get(key, 0.0):
                scores[key] = score
        return scores

    def _embedding_scores(self, texts: list[str]) -> list[dict[str, float]]:
        assert self.embedder is not None
        if self._vectors is None:
            self._vectors = self.embedder.embed([text for _, text in self._texts])
        line_vectors = self.embedder.embed([text or "-" for text in texts])
        results = []
        for vector in line_vectors:
            scores: dict[str, float] = {}
            for (key, _text), candidate in zip(self._texts, self._vectors, strict=True):
                score = cosine(vector, candidate)
                if score > scores.get(key, 0.0):
                    scores[key] = score
            results.append(scores)
        return results
