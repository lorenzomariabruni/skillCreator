#!/usr/bin/env python3
"""CLI per convertire PDF o DOCX in una famiglia di skill per agenti tramite endpoint OpenAI-compatible.

Pipeline:
  1. Converti il documento sorgente in Markdown intermedio (document_to_md).
  2. Chiama l'LLM (con few-shot) per decidere quante skill servono e restituire
     un descrittore JSON {"single": bool, "skills": [...]}.
  3a. Se single=True  → genera un unico file .md dalla skill identificata.
  3b. Se single=False → per ogni sub-skill genera un .md dedicato (con chunking
      se il documento è grande), poi genera il main skill che li referenzia.
  4. Salva tutto in <input_stem>-skills/ accanto al file sorgente.

Formato di output obbligatorio per ogni skill:

    ---
    name: skillname
    description: when to use it
    ---

    Skill body in Markdown.
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
DEFAULT_MAX_RETRIES = 3          # max retry attempts for transient LLM errors
DEFAULT_RETRY_BACKOFF = 5.0      # base backoff seconds (doubled each attempt)

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


# ---------------------------------------------------------------------------
# Skill output format (injected into every generation prompt)
# ---------------------------------------------------------------------------

SKILL_FORMAT_RULES = """\
SKILL OUTPUT FORMAT — mandatory for every skill file:

Every output file MUST start with a YAML frontmatter block:

---
name: skillname_in_snake_case
description: One sentence answering "when should an agent load this skill?".
---

Skill body in Markdown.

Rules:
- The frontmatter MUST be the very first content (no blank lines before ---).
- `name`: lowercase snake_case, no spaces, no special chars except underscores.
- `description`: one concise sentence.
- After the closing --- leave exactly one blank line, then start the body.
- Do NOT put the frontmatter syntax inside the Markdown body.
- Do NOT wrap the output in a fenced code block.
"""

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are a senior AI agent skill author.
You transform technical documents into HIGH-QUALITY reusable skill files for coding agents
(Roo Code, Claude Code, and similar).

{SKILL_FORMAT_RULES}

Core rules:
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
# Prompt: Phase A – identify skills (with few-shot examples)
# ---------------------------------------------------------------------------

IDENTIFY_SKILLS_PROMPT = """\
Analyse the technical document below and decide which skill files are needed.

RETURN ONLY a JSON object — no markdown fences, no explanation, nothing else.

Schema:
{{
  "single": <true if ONE skill file is sufficient, false if multiple are needed>,
  "skills": [
    {{
      "name": "<snake_case identifier>",
      "description": "<one sentence: when should an agent load this skill?>",
      "section_hint": "<short phrase locating this topic in the document>"
    }}
  ]
}}

Rules:
- When `single` is true, `skills` contains exactly ONE entry for the whole document.
- When `single` is false, `skills` contains 2–8 entries (merge closely related topics).
- Never return more than 8 skills.
- `name` must be lowercase snake_case.

--- FEW-SHOT EXAMPLES ---

Example 1 — simple, single-topic document:
Input document title: "Redis Cache Patterns"
Expected output:
{{"single": true, "skills": [{{"name": "redis_cache_patterns", "description": "Load when implementing or troubleshooting Redis caching strategies.", "section_hint": "entire document"}}]}}

Example 2 — multi-topic guide:
Input document title: "Python Data Engineering Handbook"
Expected output:
{{"single": false, "skills": [{{"name": "pandas_dataframe_ops", "description": "Load when performing data manipulation with pandas DataFrames.", "section_hint": "Section 2 – Pandas"}}, {{"name": "kafka_python_producer", "description": "Load when setting up a Kafka producer in Python.", "section_hint": "Section 5 – Kafka integration"}}, {{"name": "spark_job_submission", "description": "Load when submitting or tuning a PySpark job.", "section_hint": "Section 7 – Spark"}}]}}

Example 3 — broad reference manual:
Input document title: "AWS Infrastructure Runbook"
Expected output:
{{"single": false, "skills": [{{"name": "aws_vpc_setup", "description": "Load when creating or modifying a VPC configuration.", "section_hint": "Chapter 1 – Networking"}}, {{"name": "aws_iam_policies", "description": "Load when writing or auditing IAM policies.", "section_hint": "Chapter 3 – IAM"}}, {{"name": "aws_rds_backup", "description": "Load when configuring RDS automated backups or restores.", "section_hint": "Chapter 6 – Databases"}}]}}

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

{skill_format_rules}

Skill to generate:
  name        : {skill_name}
  description : {skill_description}
  topic hint  : {section_hint}

Source: {filename}  (chunk {chunk_index}/{total_chunks})

Instructions:
1. Focus ONLY on the topic above; skip unrelated content.
2. Include ALL code examples, commands, configs relevant to this topic VERBATIM.
3. Structure:
   ## Purpose
   ## When to use
   ## Core instructions
   ## Examples      ← every relevant code block from the source, verbatim
   ## Constraints
4. If this is not chunk 1, output ONLY new sections/content not already covered;
   do not repeat the frontmatter or sections already written.
5. Begin with the YAML frontmatter ONLY on chunk 1:
   name: {skill_name}
   description: {skill_description}
6. If this chunk contains NO content relevant to the topic, reply with exactly:
   NO_RELEVANT_CONTENT

Chunk:
<chunk>
{chunk_text}
</chunk>
"""

# ---------------------------------------------------------------------------
# Prompt: Phase B merge – merge chunk outputs into one skill
# ---------------------------------------------------------------------------

SUB_SKILL_MERGE_PROMPT = """\
Merge the partial skill outputs below into ONE complete, deduplicated skill file.

{skill_format_rules}

Skill metadata:
  name        : {skill_name}
  description : {skill_description}

Partial outputs (in order):
{partials}

Rules:
- Output a single skill file starting with the YAML frontmatter.
- Deduplicate aggressively; keep the most complete/authoritative version.
- Preserve ALL code blocks verbatim; never rewrite them.
- Sections: ## Purpose / ## When to use / ## Core instructions / ## Examples / ## Constraints
"""

# ---------------------------------------------------------------------------
# Prompt: Phase C – generate main skill (multi-skill mode)
# ---------------------------------------------------------------------------

MAIN_SKILL_PROMPT = """\
Generate the MAIN (index) skill file for a document broken into sub-skills.

{skill_format_rules}

Document      : {filename}
Main skill    : {main_skill_name}
Description   : {main_skill_description}

Sub-skills already generated:
{sub_skills_list}

Instructions:
1. Start with the YAML frontmatter (name: {main_skill_name}).
2. Write a concise overview of what the document covers (3-6 bullets).
3. ## Sub-skills section — for each sub-skill:
   ### <name>
   File: `<name>.md`
   Load when: <one sentence>
4. ## Quick reference — the most critical cross-cutting rules (bullet list).
5. Do NOT duplicate content from sub-skills; reference, do not repeat.
6. Keep it concise — this is a router, not a repeat of the sub-skills.

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
    name: str
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
            "Directory di output (default: <input_stem>-skills/ "
            "nella stessa cartella del file sorgente)"
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
        stem = re.sub(r"[^a-z0-9_\-]", "_", input_path.stem.lower()).strip("_-") or "skill"
        output_dir = input_path.parent / f"{stem}-skills"

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
# File reading – raw extraction
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
# HTTP  (with retry + None-guard)
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def call_llm(
    config: Config,
    messages: List[dict],
    response_format: Optional[dict] = None,
    label: str = "LLM",
) -> str:
    """
    Call the LLM endpoint and return the response content as a str.

    Raises RuntimeError if:
    - HTTP status >= 400 (after retries)
    - Response JSON is malformed
    - content field is None, empty, or not a string (after retries)

    Retries up to DEFAULT_MAX_RETRIES times with exponential backoff on:
    - HTTP 5xx errors
    - content is None or empty string
    """
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
                url,
                headers=_headers(config.api_key),
                json=payload,
                timeout=config.timeout,
            )
            elapsed = time.monotonic() - t0
            log.debug("  ← %.1fs  [%s]  status=%d", elapsed, label, resp.status_code)

            # Transient server error → retry
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            # Hard client error → fail immediately
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()

            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"Risposta malformata: {json.dumps(data)[:600]}") from exc

            # Guard: content must be a non-empty string
            if content is None:
                raise RuntimeError("content è None nella risposta LLM")
            if not isinstance(content, str):
                raise RuntimeError(
                    f"content non è una stringa: {type(content).__name__!r} — {content!r:.200}"
                )
            if not content.strip():
                raise RuntimeError("content è una stringa vuota nella risposta LLM")

            if usage := data.get("usage"):
                log.debug(
                    "  tokens prompt=%s compl=%s total=%s",
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("total_tokens", "?"),
                )

            return content  # ← always a non-empty str here

        except RuntimeError as exc:
            last_exc = exc
            is_client_error = "HTTP 4" in str(exc)
            if is_client_error or attempt == DEFAULT_MAX_RETRIES:
                raise
            backoff = DEFAULT_RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(
                "  [%s] attempt %d/%d failed: %s — retry in %.0fs",
                label, attempt, DEFAULT_MAX_RETRIES, exc, backoff,
            )
            time.sleep(backoff)

    # Should never reach here, but satisfy type checker
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
        name = re.sub(r"[^a-z0-9_]", "_", (item.get("name") or "skill").lower()).strip("_")
        desc = (item.get("description") or "").strip()
        hint = (item.get("section_hint") or "").strip()
        if name:
            descriptors.append(SubSkillDescriptor(name=name, description=desc, section_hint=hint))

    if not descriptors:
        log.warning("Nessuna skill identificata; fallback a skill unica.")
        stem = re.sub(r"[^a-z0-9_]", "_", config.input_path.stem.lower()).strip("_") or "skill"
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
        log.info("  • %-35s  %s", d.name, d.section_hint)

    return IdentifyResult(single=single, skills=descriptors)


# ---------------------------------------------------------------------------
# Phase B – generate one sub-skill (chunking + merge)
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
        result = _safe_strip(
            call_llm(
                config,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                label=f"{descriptor.name}[1/1]",
            ),
            label=f"{descriptor.name}[1/1]",
        )
        log.info("  ✓ %s  —  %.1fs  (%d chars)", descriptor.name, time.monotonic() - t0, len(result))
        return result

    # Multi-chunk: collect partials
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
                call_llm(
                    config,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    label=f"{descriptor.name}[{i}/{len(chunks)}]",
                ),
                label=f"{descriptor.name}[{i}/{len(chunks)}]",
            )
        except RuntimeError as exc:
            log.warning("  Chunk %d/%d skipped (LLM error): %s", i, len(chunks), exc)
            raw = ""

        # Skip sentinel and empty responses
        if raw and raw.strip().upper() != "NO_RELEVANT_CONTENT":
            partials.append(raw)
        else:
            log.debug("  Chunk %d/%d: no relevant content, skipped.", i, len(chunks))

        if i < len(chunks):
            time.sleep(0.2)

    if not partials:
        raise RuntimeError(
            f"Tutti i chunk per la skill '{descriptor.name}' hanno prodotto output vuoto o non rilevante."
        )

    if len(partials) == 1:
        log.info("  Solo 1 partial valido per %s — nessun merge necessario.", descriptor.name)
        return partials[0]

    # Merge
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
    merged = _safe_strip(
        call_llm(
            config,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": merge_prompt},
            ],
            label=f"{descriptor.name}[merge]",
        ),
        label=f"{descriptor.name}[merge]",
    )
    log.info("  ✓ %s merged  —  %.1fs  (%d chars)", descriptor.name, time.monotonic() - t0, len(merged))
    return merged


# ---------------------------------------------------------------------------
# Phase C – generate main skill
# ---------------------------------------------------------------------------

def generate_main_skill(
    config: Config,
    document_md: str,
    descriptors: List[SubSkillDescriptor],
) -> tuple[str, str]:
    log.info("--- Fase C: Generazione skill principale ---")
    stem = re.sub(r"[^a-z0-9_]", "_", config.input_path.stem.lower()).strip("_") or "main"
    main_desc = (
        f"Master index for {config.input_path.name}. "
        "Load to discover which sub-skill to use for a given task."
    )
    sub_list = "\n".join(
        f"- name: {d.name}\n  file: {d.name}.md\n"
        f"  description: {d.description}\n  section_hint: {d.section_hint}"
        for d in descriptors
    )
    prompt = MAIN_SKILL_PROMPT.format(
        skill_format_rules=SKILL_FORMAT_RULES,
        filename=config.input_path.name,
        main_skill_name=stem,
        main_skill_description=main_desc,
        sub_skills_list=sub_list,
        document_excerpt=document_md[:4000],
    )
    t0 = time.monotonic()
    content = _safe_strip(
        call_llm(
            config,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            label="main",
        ),
        label="main",
    )
    log.info("Skill principale pronta  —  %.1fs  (%d chars)", time.monotonic() - t0, len(content))
    return content, stem


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_skill(output_dir: Path, name: str, content: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.md"
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
        content = generate_sub_skill(config, descriptor, document_md)
        saved.append(save_skill(config.output_dir, descriptor.name, content))
    else:
        for i, descriptor in enumerate(plan.skills, 1):
            log.info("[%d/%d] %s", i, len(plan.skills), descriptor.name)
            content = generate_sub_skill(config, descriptor, document_md)
            saved.append(save_skill(config.output_dir, descriptor.name, content))
            if i < len(plan.skills):
                time.sleep(0.2)
        main_content, main_name = generate_main_skill(config, document_md, plan.skills)
        saved.append(save_skill(config.output_dir, main_name, main_content))

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
        print(f"\n✓ {len(paths)} skill generate in: {config.output_dir}/")
        for p in paths:
            print(f"  • {p.name}")
        return 0
    except Exception as exc:
        logging.getLogger("skill_generator").error("Errore fatale: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
