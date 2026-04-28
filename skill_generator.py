#!/usr/bin/env python3
"""CLI per convertire PDF o DOCX in una famiglia di skill per agenti tramite endpoint OpenAI-compatible.

Pipeline:
  1. Converti il documento sorgente in Markdown intermedio (document_to_md).
  2. Chiama l'LLM (con few-shot) per decidere quante skill servono e restituire
     un descrittore JSON {"single": bool, "skills": [...]}.
  3a. Se single=True  → genera <output_dir>/<skill-name>/SKILL.md
  3b. Se single=False → genera:
        <output_dir>/<main-skill-name>/SKILL.md            (index)
        <output_dir>/<main-skill-name>/references/<sub>.md (sub-skills)

Formato di output obbligatorio per ogni skill (Roo Code compatible):

    ---
    name: skill-name-in-kebab-case
    description: One sentence answering "when should an agent load this skill?".
    ---

    Skill body in Markdown.

Regole di naming Roo Code:
  - Il campo `name` deve corrispondere ESATTAMENTE al nome della cartella che contiene SKILL.md
  - Solo lowercase letters, numeri e trattini (hyphens). NO underscores.
  - 1–64 caratteri, no leading/trailing/consecutive hyphens.
  - Solo `name` e `description` sono campi obbligatori (no `agents`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import requests
from docx import Document
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

DEFAULT_MAX_CHARS = 12000
DEFAULT_OVERLAP = 1200
DEFAULT_TEMPERATURE = 0.1
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_OUTPUT_TOKENS = 4000
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 5.0

MAIN_SKILL_FILENAME = "SKILL.md"
REFERENCES_DIR = "references"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", "%H:%M:%S"))
    logger = logging.getLogger("skill_generator")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log: logging.Logger = logging.getLogger("skill_generator")


def _safe_strip(value: object, label: str = "LLM response") -> str:
    """Return value.strip() if value is a non-empty string, else raise RuntimeError."""
    if not isinstance(value, str):
        raise RuntimeError(
            f"{label} returned non-string content: {type(value).__name__!r} — value: {value!r:.200}"
        )
    return value.strip()


def _to_skill_name(raw: str) -> str:
    """
    Convert any string to a valid Roo Code skill name:
    - lowercase
    - only letters, digits, hyphens
    - no leading/trailing/consecutive hyphens
    - max 64 chars
    """
    s = raw.lower()
    s = re.sub(r"[_\s]+", "-", s)          # underscores/spaces -> hyphens
    s = re.sub(r"[^a-z0-9\-]", "", s)      # remove all other chars
    s = re.sub(r"-{2,}", "-", s)           # collapse consecutive hyphens
    s = s.strip("-")                        # remove leading/trailing hyphens
    s = s[:64]                              # enforce max length
    s = s.rstrip("-")                       # remove trailing hyphen after truncation
    return s or "skill"


# ---------------------------------------------------------------------------
# Roo Code output sanitiser
# ---------------------------------------------------------------------------

def sanitize_skill_output(raw: str, expected_name: str | None = None) -> str:
    """Ensure the LLM output is a Roo Code-compatible skill file.

    Fixes applied (in order):
    1. Strip an outer fenced code block wrapper (```markdown ... ``` or ``` ... ```).
    2. Remove any blank lines that appear before the opening ``---``.
    3. Parse the YAML frontmatter and keep ONLY ``name`` and ``description``.
       - Normalise ``name`` through ``_to_skill_name()``.
       - If ``expected_name`` is provided and the parsed name differs, override it.
    4. Reconstruct the frontmatter with exactly the two allowed fields.
    5. Guarantee exactly ONE blank line between the closing ``---`` and the body.

    Returns the sanitised content, or raises RuntimeError if no valid frontmatter
    can be found after all attempts.
    """
    text = raw.strip()

    # 1. Strip fenced code block wrapper
    # Handles ```markdown, ```md, ``` (with optional trailing language tag)
    fence_re = re.compile(r"^```[a-zA-Z]*\n(.*?)\n?```\s*$", re.DOTALL)
    m = fence_re.match(text)
    if m:
        text = m.group(1).strip()

    # 2. Remove blank lines before the very first ---
    lines = text.splitlines()
    start_idx = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            start_idx = i
            break
    text = "\n".join(lines[start_idx:])

    # 3 & 4. Parse frontmatter and rebuild with only name + description
    fm_re = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.DOTALL)
    fm_match = fm_re.match(text)
    if not fm_match:
        raise RuntimeError(
            "sanitize_skill_output: no valid YAML frontmatter found after sanitisation.\n"
            f"Content start: {text[:300]!r}"
        )

    fm_body = fm_match.group(1)
    body_after = text[fm_match.end():]

    # Parse individual fields from the raw frontmatter (simple key: value parsing,
    # handles multi-line description values that are indented).
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in fm_body.splitlines():
        kv = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)", line)
        if kv:
            current_key = kv.group(1)
            fields[current_key] = kv.group(2).strip().strip('"').strip("'")
        elif current_key and line.startswith((" ", "\t")):
            # continuation of previous value
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()

    raw_name = fields.get("name", "")
    description = fields.get("description", "")

    # Normalise name
    sanitised_name = _to_skill_name(raw_name) if raw_name else ""
    if expected_name:
        expected_clean = _to_skill_name(expected_name)
        if sanitised_name != expected_clean:
            log.debug(
                "sanitize_skill_output: overriding name %r → %r", sanitised_name, expected_clean
            )
            sanitised_name = expected_clean

    if not sanitised_name:
        sanitised_name = "skill"
    if not description:
        description = f"Load when working with {sanitised_name}."

    # 5. Reconstruct with exactly one blank line after closing ---
    body_after = body_after.lstrip("\n")
    sanitised = f"---\nname: {sanitised_name}\ndescription: {description}\n---\n\n{body_after}"
    return sanitised


# ---------------------------------------------------------------------------
# Skill output format (injected into every generation prompt)
# ---------------------------------------------------------------------------

SKILL_FORMAT_RULES = """\
SKILL OUTPUT FORMAT — mandatory for every skill file (Roo Code compatible):

Every output file MUST start with a YAML frontmatter block:

---
name: skill-name-in-kebab-case
description: One sentence answering "when should an agent load this skill?".
---

Skill body in Markdown.

Roo Code naming rules (CRITICAL — violations make the skill invisible):
- `name`: ONLY lowercase letters, digits, and hyphens. NO underscores, NO spaces.
  Valid: `redis-caching`, `spring-boot-setup`, `kafka-producer`
  INVALID: `redis_caching`, `Spring Boot Setup`, `kafka--producer`
- `name` must exactly match the directory name that contains SKILL.md.
- 1–64 characters; no leading, trailing, or consecutive hyphens.
- `description`: one concise sentence, no quotes around the value.
- Only `name` and `description` are required — do NOT add `agents` or other fields.
- The frontmatter MUST be the very first content — no blank lines before the opening ---.
- After the closing --- leave exactly ONE blank line, then start the Markdown body.
- Do NOT wrap the output in a fenced code block.
"""

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are an expert AI orchestrator and prompt engineer specialising in agentic skill authoring.
You transform technical documents into HIGH-QUALITY, comprehensive skill files for coding agents
(Roo Code, Claude Code, and similar).

{SKILL_FORMAT_RULES}

Core rules:
- A SHORT description is insufficient for accurate tool calling. Every skill body MUST be
  comprehensive enough that a routing agent can make an unambiguous decision.
- Preserve source intent, procedures, rules, heuristics, glossary, and examples.
- Reorganise for maximum agent usefulness; remove repetition, OCR noise, boilerplate.
- Copy ALL code examples, commands, configs, schemas, regexes, JSON, YAML, XML, SQL
  VERBATIM from the source. Never rewrite or invent code.
- Do not summarise code into prose when the code itself carries instructional value.
- Prefer imperative instructions addressed to the agent.
- No prefatory or closing commentary.
- Return Markdown only (after the frontmatter).
"""

# ---------------------------------------------------------------------------
# Prompt: Phase A – identify skills
# ---------------------------------------------------------------------------

IDENTIFY_SKILLS_PROMPT = """\
Analyse the technical document below and decide which skill files are needed.

RETURN ONLY a JSON object — no markdown fences, no explanation, nothing else.

Schema:
{{
  "single": <true if ONE skill file is sufficient, false if multiple are needed>,
  "skills": [
    {{
      "name": "<kebab-case identifier: lowercase letters, digits, hyphens ONLY>",
      "description": "<one sentence: when should an agent load this skill?>",
      "section_hint": "<short phrase locating this topic in the document>"
    }}
  ]
}}

Rules:
- When `single` is true, `skills` contains exactly ONE entry for the whole document.
- When `single` is false, `skills` contains 2–8 entries (merge closely related topics).
- Never return more than 8 skills.
- `name` must be kebab-case: lowercase, letters/digits/hyphens only. NO underscores.

--- FEW-SHOT EXAMPLES ---

Example 1 — simple, single-topic document:
Input document title: "Redis Cache Patterns"
Expected output:
{{"single": true, "skills": [{{"name": "redis-cache-patterns", "description": "Load when implementing or troubleshooting Redis caching strategies.", "section_hint": "entire document"}}]}}

Example 2 — multi-topic guide:
Input document title: "Python Data Engineering Handbook"
Expected output:
{{"single": false, "skills": [{{"name": "pandas-dataframe-ops", "description": "Load when performing data manipulation with pandas DataFrames.", "section_hint": "Section 2 – Pandas"}}, {{"name": "kafka-python-producer", "description": "Load when setting up a Kafka producer in Python.", "section_hint": "Section 5 – Kafka integration"}}, {{"name": "spark-job-submission", "description": "Load when submitting or tuning a PySpark job.", "section_hint": "Section 7 – Spark"}}]}}

Example 3 — broad reference manual:
Input document title: "AWS Infrastructure Runbook"
Expected output:
{{"single": false, "skills": [{{"name": "aws-vpc-setup", "description": "Load when creating or modifying a VPC configuration.", "section_hint": "Chapter 1 – Networking"}}, {{"name": "aws-iam-policies", "description": "Load when writing or auditing IAM policies.", "section_hint": "Chapter 3 – IAM"}}, {{"name": "aws-rds-backup", "description": "Load when configuring RDS automated backups or restores.", "section_hint": "Chapter 6 – Databases"}}]}}

--- END OF EXAMPLES ---

Source filename : {filename}
Source type     : {source_type}

Document (Markdown):
<document>
{document_md}
</document>
"""

# ---------------------------------------------------------------------------
# Prompt: Phase B – generate one sub-skill from one chunk
# ---------------------------------------------------------------------------

SUB_SKILL_CHUNK_PROMPT = """\
Generate (or continue) a dedicated skill file for the topic below, using ONLY the
content in the document chunk provided.

A short description is NOT sufficient for accurate agentic routing. The skill body
must be comprehensive enough that a routing agent can make an unambiguous decision.

{skill_format_rules}

Skill to generate:
  name        : {skill_name}
  description : {skill_description}
  topic hint  : {section_hint}

Source: {filename}  (chunk {chunk_index}/{total_chunks})

Instructions:
1. Focus ONLY on the topic above; skip unrelated content.
2. Include ALL code examples, commands, configs relevant to this topic VERBATIM.
3. Structure the Markdown body with the following sections IN ORDER:
   ## Core Purpose
   A concise 1-2 sentence explanation of what this skill accomplishes.

   ## When to Use
   Explicit bullet-point rules defining the exact user intents, contexts, or data
   requirements that mandate calling this skill.

   ## When NOT to Use
   Explicit bullet-point boundaries explaining when the agent should bypass this skill
   (e.g., when internal knowledge suffices, or a different skill is more appropriate).

   ## Example Triggers
   3 to 5 distinct, realistic examples of user queries that should invoke this skill.
   Format each as:
   - "<user query>" -> Use this skill to <action>.

   ## Core Instructions
   Step-by-step procedures, rules, heuristics, and configuration details.

   ## Examples
   Every relevant code block from the source, verbatim.

   ## Constraints
   Hard limits, caveats, known edge cases, and version requirements.

4. If this is not chunk 1, output ONLY new sections/content not already covered;
   do not repeat the frontmatter or sections already written.
5. Begin with the YAML frontmatter ONLY on chunk 1:
   ---
   name: {skill_name}
   description: {skill_description}
   ---
   (name must use hyphens only, NO underscores)
6. If this chunk contains NO content relevant to the topic, reply with exactly:
   NO_RELEVANT_CONTENT

Chunk:
<chunk>
{chunk_text}
</chunk>
"""

# ---------------------------------------------------------------------------
# Prompt: Phase B merge
# ---------------------------------------------------------------------------

SUB_SKILL_MERGE_PROMPT = """\
Merge the partial skill outputs below into ONE complete, deduplicated skill file.

A short description is NOT sufficient for accurate agentic routing. The merged output
must be comprehensive enough that a routing agent can make an unambiguous decision.

{skill_format_rules}

Skill metadata:
  name        : {skill_name}
  description : {skill_description}

Partial outputs (in order):
{partials}

Rules:
- Output a single skill file starting with the YAML frontmatter (name + description only).
- The very first line must be --- (opening frontmatter delimiter).
- name must use hyphens only (NO underscores).
- Deduplicate aggressively; keep the most complete/authoritative version of each section.
- Preserve ALL code blocks verbatim; never rewrite them.
- The Markdown body MUST contain the following sections IN ORDER:
  ## Core Purpose        — 1-2 sentence explanation of what the skill accomplishes.
  ## When to Use         — explicit bullet-point trigger conditions.
  ## When NOT to Use     — explicit bullet-point negative constraints.
  ## Example Triggers    — 3 to 5 user query examples formatted as:
                           - "<query>" -> Use this skill to <action>.
  ## Core Instructions   — step-by-step procedures, rules, heuristics.
  ## Examples            — all relevant code blocks from the source, verbatim.
  ## Constraints         — hard limits, caveats, edge cases, version requirements.
"""

# ---------------------------------------------------------------------------
# Prompt: Phase C – generate main SKILL.md
# ---------------------------------------------------------------------------

MAIN_SKILL_SYSTEM_PROMPT = """\
You are an expert AI orchestrator and prompt engineer. Your task is to expand a basic
skill outline into a highly optimised tool description for an LLM agent. This description
will be saved in SKILL.md and used by the model's router to decide when to call the tool.

A short description is insufficient for accurate tool calling. You must generate a
comprehensive description that leaves no ambiguity for the routing agent.
"""

MAIN_SKILL_PROMPT = """\
Generate the MAIN (index) skill file SKILL.md for a document broken into sub-skills.

{skill_format_rules}

Based on the provided information, generate a SKILL.md description that includes the
following sections:

1. Core Purpose: A concise, 1-2 sentence explanation of what the tool accomplishes.
2. When to Use (Trigger Conditions): Explicit bullet-point instructions defining the
   exact user intents, contexts, or data requirements that mandate calling this tool.
3. When NOT to Use (Negative Constraints): Explicit bullet-point boundaries explaining
   when the model should bypass this tool (e.g., relying on internal knowledge or a
   different tool instead).
4. Example Triggers: Provide 3 to 5 distinct, realistic examples of user queries that
   should result in calling this tool. Format each as:
   - "<user query>" -> Use this skill to <action>.
5. Sub-skills index: for each sub-skill list name, file path, and a one-sentence
   "Load when:" description.
6. Quick Reference: the most critical cross-cutting rules as a bullet list.

Input Data:

Document      : {filename}
Main skill    : {main_skill_name}
Description   : {main_skill_description}

Sub-skills already generated (saved under references/):
{sub_skills_list}

Additional instructions:
- Start with the YAML frontmatter (name + description only, no other fields):
  ---
  name: {main_skill_name}
  description: {main_skill_description}
  ---
  (name must use hyphens only, NO underscores)
- The very first line of the output must be --- (the opening frontmatter delimiter).
- Do NOT duplicate content already in sub-skills; reference, do not repeat.
- Output only the formatted Markdown content for the SKILL.md file.

Document excerpt (first 4000 chars):
<excerpt>
{document_excerpt}
</excerpt>
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubSkillDescriptor:
    name: str          # validated kebab-case
    description: str
    section_hint: str


@dataclass
class IdentifyResult:
    single: bool
    skills: List[SubSkillDescriptor]


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    input_path: Path
    output_dir: Path
    max_chars: int = DEFAULT_MAX_CHARS
    overlap: int = DEFAULT_OVERLAP
    temperature: float = DEFAULT_TEMPERATURE
    timeout: int = DEFAULT_TIMEOUT
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    verbose: bool = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> Config:
    parser = argparse.ArgumentParser(
        description="Converte PDF/DOCX in skill Markdown via endpoint OpenAI-compatible."
    )
    parser.add_argument("input_file", help="File sorgente .pdf o .docx")
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help=(
            "Directory radice di output. "
            "Default: cartella nella stessa posizione del file sorgente. "
            "Può puntare direttamente a ~/.roo/skills/ o .roo/skills/ "
            "così le skill sono immediatamente disponibili in Roo Code."
        ),
    )
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("-v", "--verbose", action="store_true", default=False)

    args = parser.parse_args(argv)

    if args.max_chars < 1000:
        raise ValueError("--max-chars deve essere almeno 1000")
    if not args.base_url or not args.api_key or not args.model:
        raise ValueError("base-url, api-key e model sono obbligatori (o via env OPENAI_*)")

    input_path = Path(args.input_file)
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        raw_stem = re.sub(r"[^a-z0-9_\-]", "_", input_path.stem.lower()).strip("_-") or "skill"
        output_dir = input_path.parent / f"{raw_stem}-skills"

    return Config(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        model=args.model,
        input_path=input_path,
        output_dir=output_dir,
        max_chars=args.max_chars,
        overlap=args.overlap,
        temperature=args.temperature,
        timeout=args.timeout,
        max_output_tokens=args.max_output_tokens,
        verbose=args.verbose,
    )


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def read_raw_text(input_path: Path) -> str:
    if not input_path.exists():
        raise FileNotFoundError(f"File non trovato: {input_path}")
    suffix = input_path.suffix.lower()
    log.info("Lettura file: %s  (%s)", input_path.name, suffix.lstrip(".").upper())
    if suffix == ".pdf":
        return _extract_pdf(input_path)
    if suffix == ".docx":
        return _extract_docx(input_path)
    raise ValueError(f"Formato non supportato: {suffix}. Usa .pdf o .docx")


def _extract_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    log.info("PDF: %d pagine", len(reader.pages))
    parts: List[str] = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
        parts.append(f"\n\n## Page {i}\n\n{text}")
    return "".join(parts)


def _iter_docx_blocks(parent):
    root = parent.element.body if isinstance(parent, _Document) else parent._tc
    for child in root.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _extract_docx(path: Path) -> str:
    log.info("Apertura DOCX ...")
    doc = Document(str(path))
    blocks: List[str] = []
    for block in _iter_docx_blocks(doc):
        if isinstance(block, Paragraph):
            t = block.text.strip()
            if t:
                style = (block.style.name or "").lower()
                if style.startswith("heading"):
                    try:
                        level = int(style.split()[-1])
                    except ValueError:
                        level = 2
                    blocks.append("#" * max(1, min(level, 6)) + f" {t}")
                else:
                    blocks.append(t)
        elif isinstance(block, Table):
            rows = block.rows
            if not rows:
                continue
            md_rows: List[str] = []
            for r_idx, row in enumerate(rows):
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                md_rows.append("| " + " | ".join(cells) + " |")
                if r_idx == 0:
                    md_rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
            blocks.append("\n".join(md_rows))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Document → clean Markdown
# ---------------------------------------------------------------------------

def document_to_md(raw_text: str) -> str:
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u00a0", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = text.split("\n")
    result: List[str] = []
    in_code = False

    for line in lines:
        stripped = line.rstrip()
        is_code_line = (
            re.match(r"^    \S", stripped)
            or re.match(r"^\$\s", stripped)
            or re.match(r"^>>> ", stripped)
        )
        if is_code_line and not in_code:
            result.append("```")
            in_code = True
        elif not is_code_line and in_code:
            result.append("```")
            in_code = False
        result.append(stripped)

    if in_code:
        result.append("```")

    return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            split = text.rfind("\n\n", start, end)
            if split == -1 or split <= start + max_chars // 3:
                split = text.rfind("\n", start, end)
            if split != -1 and split > start:
                end = split
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(0, end - overlap)
    log.info("Chunking: %d chunk  (max_chars=%d, overlap=%d)", len(chunks), max_chars, overlap)
    for i, c in enumerate(chunks, 1):
        log.debug("  Chunk %d: %d chars", i, len(c))
    return chunks


# ---------------------------------------------------------------------------
# HTTP (with retry + None-guard)
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def call_llm(
    config: Config,
    messages: List[dict],
    response_format: Optional[dict] = None,
    label: str = "LLM",
) -> str:
    payload: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    url = f"{config.base_url}/chat/completions"
    last_exc: Optional[Exception] = None

    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        try:
            log.debug("  → POST %s  [%s]  attempt %d/%d", url, label, attempt, DEFAULT_MAX_RETRIES)
            t0 = time.monotonic()
            resp = requests.post(
                url, headers=_headers(config.api_key), json=payload, timeout=config.timeout,
            )
            elapsed = time.monotonic() - t0
            log.debug("  ← %.1fs  [%s]  status=%d", elapsed, label, resp.status_code)

            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"Risposta malformata: {json.dumps(data)[:600]}") from exc

            if content is None:
                raise RuntimeError("content è None nella risposta LLM")
            if not isinstance(content, str):
                raise RuntimeError(f"content non è str: {type(content).__name__!r}")
            if not content.strip():
                raise RuntimeError("content è vuoto nella risposta LLM")

            if usage := data.get("usage"):
                log.debug(
                    "  tokens prompt=%s compl=%s total=%s",
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("total_tokens", "?"),
                )
            return content

        except RuntimeError as exc:
            last_exc = exc
            if "HTTP 4" in str(exc) or attempt == DEFAULT_MAX_RETRIES:
                raise
            backoff = DEFAULT_RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning("  [%s] attempt %d/%d failed: %s — retry in %.0fs",
                        label, attempt, DEFAULT_MAX_RETRIES, exc, backoff)
            time.sleep(backoff)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase A – identify skills
# ---------------------------------------------------------------------------

def identify_skills(config: Config, document_md: str) -> IdentifyResult:
    log.info("--- Fase A: Identificazione skill ---")
    excerpt = document_md[:config.max_chars]

    prompt = IDENTIFY_SKILLS_PROMPT.format(
        filename=config.input_path.name,
        source_type=config.input_path.suffix.lower().lstrip("."),
        document_md=excerpt,
    )

    raw = call_llm(
        config,
        messages=[
            {"role": "system", "content": "You are a precise JSON-only responder. Output valid JSON with no extra text."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        label="identify",
    )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON non valido nella fase A: {raw[:600]}") from exc

    if isinstance(parsed, list):
        raw_list = parsed
        single = len(parsed) == 1
    else:
        single = bool(parsed.get("single", False))
        raw_list = parsed.get("skills") or []
        if not raw_list:
            raw_list = next((v for v in parsed.values() if isinstance(v, list)), [])

    descriptors: List[SubSkillDescriptor] = []
    for item in raw_list:
        raw_name = (item.get("name") or "skill")
        name = _to_skill_name(raw_name)   # enforce kebab-case + Roo Code rules
        desc = (item.get("description") or "").strip()
        hint = (item.get("section_hint") or "").strip()
        if name:
            descriptors.append(SubSkillDescriptor(name=name, description=desc, section_hint=hint))

    if not descriptors:
        log.warning("Nessuna skill identificata; fallback a skill unica.")
        stem = _to_skill_name(config.input_path.stem)
        descriptors.append(SubSkillDescriptor(
            name=stem,
            description=f"Use when working with {config.input_path.name}.",
            section_hint="entire document",
        ))
        single = True

    if len(descriptors) == 1:
        single = True

    log.info("Modalità: %s  |  skill identificate: %d",
             "SINGOLA" if single else "MULTI", len(descriptors))
    for d in descriptors:
        log.info("  • %-40s  %s", d.name, d.section_hint)

    return IdentifyResult(single=single, skills=descriptors)


# ---------------------------------------------------------------------------
# Phase B – generate one sub-skill
# ---------------------------------------------------------------------------

def generate_sub_skill(config: Config, descriptor: SubSkillDescriptor, document_md: str) -> str:
    log.info("  ▶ Generazione skill: %s", descriptor.name)
    chunks = chunk_text(document_md, config.max_chars, config.overlap)

    if len(chunks) == 1:
        prompt = SUB_SKILL_CHUNK_PROMPT.format(
            skill_format_rules=SKILL_FORMAT_RULES,
            skill_name=descriptor.name,
            skill_description=descriptor.description,
            section_hint=descriptor.section_hint,
            filename=config.input_path.name,
            chunk_index=1,
            total_chunks=1,
            chunk_text=chunks[0],
        )
        t0 = time.monotonic()
        raw = _safe_strip(
            call_llm(config,
                     messages=[{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": prompt}],
                     label=f"{descriptor.name}[1/1]"),
            label=f"{descriptor.name}[1/1]",
        )
        result = sanitize_skill_output(raw, expected_name=descriptor.name)
        log.info("  ✓ %s  —  %.1fs  (%d chars)", descriptor.name, time.monotonic() - t0, len(result))
        return result

    partials: List[str] = []
    for i, chunk in enumerate(chunks, 1):
        log.info("  [%d/%d] Chunk di %s ...", i, len(chunks), descriptor.name)
        prompt = SUB_SKILL_CHUNK_PROMPT.format(
            skill_format_rules=SKILL_FORMAT_RULES,
            skill_name=descriptor.name,
            skill_description=descriptor.description,
            section_hint=descriptor.section_hint,
            filename=config.input_path.name,
            chunk_index=i,
            total_chunks=len(chunks),
            chunk_text=chunk,
        )
        try:
            raw = _safe_strip(
                call_llm(config,
                         messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": prompt}],
                         label=f"{descriptor.name}[{i}/{len(chunks)}]"),
                label=f"{descriptor.name}[{i}/{len(chunks)}]",
            )
        except RuntimeError as exc:
            log.warning("  Chunk %d/%d skipped (LLM error): %s", i, len(chunks), exc)
            raw = ""

        if raw and raw.strip().upper() != "NO_RELEVANT_CONTENT":
            # Only sanitize chunk 1 (has frontmatter); subsequent chunks are raw body text
            if i == 1:
                try:
                    raw = sanitize_skill_output(raw, expected_name=descriptor.name)
                except RuntimeError as exc:
                    log.warning("  Chunk 1 sanitize failed (%s), keeping raw output.", exc)
            partials.append(raw)
        else:
            log.debug("  Chunk %d/%d: no relevant content, skipped.", i, len(chunks))

        if i < len(chunks):
            time.sleep(0.2)

    if not partials:
        raise RuntimeError(
            f"Tutti i chunk per '{descriptor.name}' hanno prodotto output vuoto."
        )

    if len(partials) == 1:
        log.info("  Solo 1 partial valido per %s.", descriptor.name)
        # Already sanitized above (was chunk 1)
        return partials[0]

    log.info("  Merge %d parti per skill %s ...", len(partials), descriptor.name)
    numbered = "\n\n".join(
        f"--- PARTIAL {i + 1}/{len(partials)} ---\n{p}" for i, p in enumerate(partials)
    )
    merge_prompt = SUB_SKILL_MERGE_PROMPT.format(
        skill_format_rules=SKILL_FORMAT_RULES,
        skill_name=descriptor.name,
        skill_description=descriptor.description,
        partials=numbered,
    )
    t0 = time.monotonic()
    merged_raw = _safe_strip(
        call_llm(config,
                 messages=[{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": merge_prompt}],
                 label=f"{descriptor.name}[merge]"),
        label=f"{descriptor.name}[merge]",
    )
    merged = sanitize_skill_output(merged_raw, expected_name=descriptor.name)
    log.info("  ✓ %s merged  —  %.1fs  (%d chars)", descriptor.name, time.monotonic() - t0, len(merged))
    return merged


# ---------------------------------------------------------------------------
# Phase C – generate main SKILL.md
# ---------------------------------------------------------------------------

def generate_main_skill(
    config: Config,
    document_md: str,
    descriptors: List[SubSkillDescriptor],
    main_name: str,
) -> str:
    log.info("--- Fase C: Generazione SKILL.md principale ---")

    # Build a semantic description from the sub-skill descriptions rather than a
    # generic placeholder, so the frontmatter field is a meaningful routing sentence.
    if len(descriptors) == 1:
        main_desc = descriptors[0].description
    else:
        topics = ", ".join(d.name.replace("-", " ") for d in descriptors[:4])
        suffix = f" and {len(descriptors) - 4} more" if len(descriptors) > 4 else ""
        main_desc = (
            f"Load to route tasks related to {topics}{suffix} "
            f"from {config.input_path.name}; see sub-skills for precise trigger conditions."
        )

    sub_list = "\n".join(
        f"- name: {d.name}\n  file: references/{d.name}.md\n"
        f"  description: {d.description}\n  section_hint: {d.section_hint}"
        for d in descriptors
    )
    prompt = MAIN_SKILL_PROMPT.format(
        skill_format_rules=SKILL_FORMAT_RULES,
        filename=config.input_path.name,
        main_skill_name=main_name,
        main_skill_description=main_desc,
        sub_skills_list=sub_list,
        document_excerpt=document_md[:4000],
    )
    t0 = time.monotonic()
    raw = _safe_strip(
        call_llm(config,
                 messages=[{"role": "system", "content": MAIN_SKILL_SYSTEM_PROMPT},
                           {"role": "user", "content": prompt}],
                 label="main"),
        label="main",
    )
    content = sanitize_skill_output(raw, expected_name=main_name)
    log.info("SKILL.md pronto  —  %.1fs  (%d chars)", time.monotonic() - t0, len(content))
    return content


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_main_skill(skill_dir: Path, content: str) -> Path:
    """
    Save SKILL.md inside skill_dir.
    skill_dir must be named after the skill (e.g. ~/.roo/skills/<skill-name>/).
    A final sanitization pass is applied before writing as a safety net.
    """
    # Derive expected name from the directory name (last component)
    expected_name = skill_dir.name
    try:
        content = sanitize_skill_output(content, expected_name=expected_name)
    except RuntimeError as exc:
        log.warning("save_main_skill: sanitize failed (%s), writing content as-is.", exc)

    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / MAIN_SKILL_FILENAME
    path.write_text(content, encoding="utf-8")
    log.info("  ✓ Salvato: %s", path)
    return path


def save_sub_skill(skill_dir: Path, name: str, content: str) -> Path:
    """Save references/<name>.md inside the main skill directory.
    A final sanitization pass is applied before writing as a safety net.
    """
    try:
        content = sanitize_skill_output(content, expected_name=name)
    except RuntimeError as exc:
        log.warning("save_sub_skill(%s): sanitize failed (%s), writing content as-is.", name, exc)

    ref_dir = skill_dir / REFERENCES_DIR
    ref_dir.mkdir(parents=True, exist_ok=True)
    path = ref_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    log.info("  ✓ Salvato: %s", path)
    return path


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def generate_skills(config: Config) -> List[Path]:
    t0 = time.monotonic()
    log.info("=== Skill Generator avviato ===")
    log.info("Sorgente  : %s", config.input_path)
    log.info("Output    : %s", config.output_dir)
    log.info("Modello   : %s @ %s", config.model, config.base_url)

    log.info("--- 1/4  Estrazione testo ---")
    raw = read_raw_text(config.input_path)

    log.info("--- 2/4  Conversione in Markdown ---")
    document_md = document_to_md(raw)
    log.info("Markdown intermedio: %d chars", len(document_md))

    log.info("--- 3/4  Identificazione skill ---")
    plan = identify_skills(config, document_md)

    log.info("--- 4/4  Generazione file skill ---")
    saved: List[Path] = []

    if plan.single:
        descriptor = plan.skills[0]
        # Skill dir = <output_dir>/<skill-name>/
        skill_dir = config.output_dir / descriptor.name
        content = generate_sub_skill(config, descriptor, document_md)
        saved.append(save_main_skill(skill_dir, content))
    else:
        # Derive main skill name from input file stem
        main_name = _to_skill_name(config.input_path.stem)
        skill_dir = config.output_dir / main_name

        # Generate sub-skills first
        for i, descriptor in enumerate(plan.skills, 1):
            log.info("[%d/%d] %s", i, len(plan.skills), descriptor.name)
            content = generate_sub_skill(config, descriptor, document_md)
            saved.append(save_sub_skill(skill_dir, descriptor.name, content))
            if i < len(plan.skills):
                time.sleep(0.2)

        # Generate and save SKILL.md index
        main_content = generate_main_skill(config, document_md, plan.skills, main_name)
        saved.append(save_main_skill(skill_dir, main_content))

    log.info("=== Completato in %.1fs — %d file generati ===", time.monotonic() - t0, len(saved))
    return saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_args(argv if argv is not None else sys.argv[1:])
        global log
        log = setup_logging(verbose=config.verbose)
        paths = generate_skills(config)

        # Determine the skill root for the summary message
        if paths:
            skill_root = paths[0].parent
            # In multi mode SKILL.md is last; its parent is the skill dir
            # In single mode the only file is SKILL.md inside the skill dir
            for p in paths:
                if p.name == MAIN_SKILL_FILENAME:
                    skill_root = p.parent
                    break

        print(f"\n✓ {len(paths)} file generati.")
        print(f"  Skill directory: {skill_root}")
        print(f"  Per usarla in Roo Code copia/sposta la cartella in:")
        print(f"    Global : ~/.roo/skills/{skill_root.name}/")
        print(f"    Project: .roo/skills/{skill_root.name}/")
        for p in paths:
            try:
                display = p.relative_to(config.output_dir)
            except ValueError:
                display = p
            print(f"  • {display}")
        return 0
    except Exception as exc:
        logging.getLogger("skill_generator").error("Errore fatale: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
