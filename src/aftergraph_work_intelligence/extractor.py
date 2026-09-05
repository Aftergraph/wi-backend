from __future__ import annotations

import hashlib
import re

from .models import ObservationInput, WorkCandidate

_TOKEN_RE = re.compile(r"[\wæøåÆØÅ]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_DANISH_ACTION = {
    "skal", "husk", "mangler", "mangle", "send", "sende", "ring", "ringe",
    "book", "booke", "køb", "købe", "fix", "fikse", "opdater", "opdatere",
    "følg", "følge", "betal", "betale", "bestil", "bestille", "kontakt",
    "kontakte", "svar", "svare", "bekræft", "bekræfte", "aftal", "aftale",
}
_ENGLISH_ACTION = {
    "must", "need", "needs", "remember", "send", "call", "book", "buy",
    "fix", "update", "follow", "pay", "order", "contact", "reply", "confirm",
    "schedule", "assign", "create", "review", "merge", "push", "deploy",
    "publish", "failed", "failure", "approve",
}
_COMPLETION_PATTERNS = [
    r"\ber sendt\b", r"\bhar sendt\b", r"\bblev sendt\b", r"\ber udført\b",
    r"\ber færdig\b", r"\bfærdiggjort\b", r"\bcompleted\b", r"\bis done\b",
    r"\bhas been sent\b", r"\bwas sent\b", r"\bresolved\b",
]
_PRIORITY_CRITICAL = {"urgent", "asap", "straks", "kritisk", "critical", "akut"}
_PRIORITY_HIGH = {"vigtigt", "important", "høj", "high", "prioritet"}
_STOPWORDS = {
    "vi", "jeg", "du", "i", "de", "den", "det", "der", "en", "et", "at", "til",
    "på", "for", "før", "efter", "med", "og", "eller", "som", "skal", "husk", "stadig",
    "we", "you", "they", "the", "a", "an", "to", "before", "after", "with",
    "and", "or", "that", "this", "must", "need", "needs", "remember", "please", "tomorrow",
    "today", "kunden", "customer",
}
_ALIASES = {
    "sende": "send", "sender": "send", "sendt": "send",
    "ringe": "ring", "ringer": "ring",
    "booke": "book", "booker": "book",
    "købe": "køb", "køber": "køb",
    "fikse": "fix", "fixe": "fix",
    "opdatere": "opdater", "opdaterer": "opdater",
    "følge": "følg", "følger": "følg",
    "betale": "betal", "betaler": "betal",
    "bestille": "bestil", "bestiller": "bestil",
    "kontakte": "kontakt", "kontakter": "kontakt",
    "svare": "svar", "svarer": "svar",
    "bekræfte": "bekræft", "bekræfter": "bekræft",
    "aftale": "aftal", "aftaler": "aftal",
    "missing": "mangler", "mangle": "mangler",
    "followup": "follow", "follow-up": "follow",
}


class RuleExtractor:
    """Deterministic baseline extractor for explicit Danish/English work signals."""

    def extract(self, observation: ObservationInput) -> WorkCandidate | None:
        text = _SPACE_RE.sub(" ", observation.text.strip())
        if not text:
            return None
        lower = text.casefold()
        if any(re.search(pattern, lower) for pattern in _COMPLETION_PATTERNS):
            return None

        tokens = [self._canonical_token(t.casefold()) for t in _TOKEN_RE.findall(text)]
        token_set = set(tokens)
        action_hits = token_set.intersection(_DANISH_ACTION | _ENGLISH_ACTION)
        explicit_need = bool(re.search(r"\b(need to|needs to|have to|skal|husk at|mangler|follow up)\b", lower))
        if not action_hits and not explicit_need:
            return None

        priority = self._priority(observation.priority_hint, token_set)
        due_hint = observation.due_hint or self._due_hint(lower)
        title = observation.title_hint or self._title(text)
        canonical_tokens = self.canonical_tokens(title + " " + text)
        if not canonical_tokens:
            canonical_tokens = tuple(sorted(token_set))
        key = hashlib.sha256(" ".join(canonical_tokens).encode("utf-8")).hexdigest()[:24]
        confidence = 0.90 if explicit_need or "skal" in token_set or "must" in token_set else 0.78
        if priority == "critical":
            confidence = max(confidence, 0.90)

        return WorkCandidate(
            title=title,
            summary=text,
            next_action=text,
            priority=priority,
            owner=observation.owner_hint,
            due_hint=due_hint,
            confidence=confidence,
            canonical_key=key,
            canonical_tokens=canonical_tokens,
            reason="explicit commitment/request baseline",
        )

    def canonical_tokens(self, text: str) -> tuple[str, ...]:
        tokens: set[str] = set()
        for raw in _TOKEN_RE.findall(text.casefold()):
            token = self._canonical_token(raw)
            if len(token) < 2 or token in _STOPWORDS:
                continue
            tokens.add(token)
        return tuple(sorted(tokens))

    @staticmethod
    def similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
        left, right = set(a), set(b)
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    @staticmethod
    def _canonical_token(token: str) -> str:
        return _ALIASES.get(token, token)

    @staticmethod
    def _priority(hint: str | None, tokens: set[str]) -> str:
        if hint:
            normalized = hint.casefold()
            if normalized in {"low", "medium", "high", "critical"}:
                return normalized
        if tokens.intersection(_PRIORITY_CRITICAL):
            return "critical"
        if tokens.intersection(_PRIORITY_HIGH):
            return "high"
        return "medium"

    @staticmethod
    def _due_hint(lower: str) -> str | None:
        patterns = [
            r"\bfør\s+(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\b",
            r"\bsenest\s+[^,.!?;]+",
            r"\bi morgen\b",
            r"\btomorrow\b",
            r"\btoday\b",
            r"\binden\s+[^,.!?;]+",
            r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, lower)
            if match:
                return match.group(0).strip()
        return None

    @staticmethod
    def _title(text: str) -> str:
        cleaned = re.sub(r"^(urgent|asap|kritisk|vigtigt)\s*:\s*", "", text, flags=re.IGNORECASE)
        cleaned = cleaned.strip(" .!?")
        if len(cleaned) <= 96:
            return cleaned
        return cleaned[:93].rstrip() + "..."
