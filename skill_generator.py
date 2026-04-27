#!/usr/bin/env python3
"""CLI per convertire PDF o DOCX in una famiglia di skill per agenti tramite endpoint OpenAI-compatible.

Pipeline:
  1. Estrai il testo dal documento sorgente.
  2. Chiama l'LLM per identificare i sub-skill presenti nel documento.
  3. Per ogni sub-skill individuato chiama l'LLM e genera un file .md dedicato.
  4. Genera una skill principale che referenzia tutti i sub-skill.

Formato di output obbligatorio per ogni skill (principale e sub-skill):

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence

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

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configura e restituisce il logger principale dell'applicazione."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger = logging.getLogger("skill_generator")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log: logging.Logger = logging.getLogger("skill_generator")

# ---------------------------------------------------------------------------
# Skill output format rules (injected into every prompt)
# ---------------------------------------------------------------------------

SKILL_FORMAT_RULES = """\
SKILL OUTPUT FORMAT (mandatory for every skill file you produce):
Every skill Markdown file MUST begin with a YAML frontmatter block followed by the skill body.
The exact format is:

---
name: skillname_in_snake_case
description: One sentence describing WHEN an agent should load this skill.
---

Skill body here in Markdown.

Rules:
- The frontmatter MUST be the very first content in the file (no blank lines before ---).
- `name` must be a lowercase snake_case identifier, no spaces, no special chars except underscores.
- `description` must answer "when should I load this skill?" in one concise sentence.
- After the closing --- leave exactly one blank line, then start the Markdown body.
- Do NOT include the frontmatter block syntax inside the Markdown body.
- Do NOT wrap the entire output in a fenced code block.
"""

# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are a senior AI agent skill author.
Your task is to transform source material extracted from PDF or DOCX files into HIGH-QUALITY reusable skill files that can be imported into coding agents such as Roo Code, Claude Code style agents, or similar systems.

{SKILL_FORMAT_RULES}

Core objective:
- Produce self-contained skill documents in Markdown.
- Preserve the source intent, procedures, rules, heuristics, glossary, and examples.
- Reorganize the content for maximum usefulness to another AI agent.
- Remove repetition, OCR noise, boilerplate page furniture, and irrelevant fragments.

Critical preservation rules:
- If the source contains code examples, commands, config blocks, schemas, prompts, regexes, JSON, YAML, XML, SQL, or pseudocode, preserve them EXACTLY — copy them verbatim from the input, never rewrite or invent them.
- Do not rewrite code examples unless the source itself contains obvious OCR corruption; if corruption is suspected, explicitly mark the snippet as possibly corrupted instead of silently fixing it.
- Preserve parameter names, flags, environment variables, identifiers, paths, URLs, and API shapes exactly as found.
- Do not summarize code into prose when the code itself carries instructional value.

Optimization rules:
- Prefer imperative instructions addressed to the agent.
- Convert descriptive material into operational rules.
- Make the skill specific, concrete, and execution-oriented.
- Merge duplicates; keep the most authoritative formulation.
- Preserve domain terminology.
- Do not invent features, APIs, commands, or examples not supported by the source.

Style rules:
- Clear, dense, agent-oriented writing.
- Minimal fluff.
- High information density.
- Markdown only (after the YAML frontmatter).
- No prefatory commentary.
- No closing commentary.
"""

# --------------- Phase A: identify sub-skills ---------------

IDENTIFY_SKILLS_PROMPT = """\
You are analyzing a technical document to identify the distinct topics or areas that each deserve their own dedicated skill file.

Source filename: {filename}
Source type: {source_type}

Read the document text below and return a JSON array of sub-skill descriptors.
Each descriptor is an object with:
  - "name": snake_case identifier for the skill (e.g. "kafka_producer_setup")
  - "description": one sentence — when should an agent load this skill?
  - "section_hint": a short phrase that locates this topic in the document (e.g. "Section 3 – Kafka configuration")

Return ONLY a valid JSON array (no markdown fences, no extra keys).
Aim for 2-8 sub-skills; merge closely related topics into one skill.

Document text:
<document>
{document_text}
</document>
"""

# --------------- Phase B: generate one sub-skill ---------------

SUB_SKILL_PROMPT = """\
You are generating a dedicated skill file for ONE specific topic extracted from a larger document.

{skill_format_rules}

Sub-skill to generate:
  name        : {skill_name}
  description : {skill_description}
  topic hint  : {section_hint}

Source filename: {filename}
Source type: {source_type}

Instructions:
1. Focus ONLY on the topic described above; ignore unrelated content in the document.
2. Extract ALL code examples, commands, configs, schemas relevant to this topic and include them VERBATIM (copy-paste from the document, never rewrite).
3. Structure the body with these sections:
   ## Purpose
   ## When to use
   ## Core instructions
   ## Examples
   ## Constraints
4. The Examples section must contain every relevant code block from the source, preserved exactly.
5. Begin the file with the YAML frontmatter as specified in the format rules, using:
   name: {skill_name}
   description: {skill_description}

Document text:
<document>
{document_text}
</document>
"""

# --------------- Phase C: generate main skill ---------------

MAIN_SKILL_PROMPT = """\
You are generating the MAIN skill file for a document that has been broken into sub-skills.

{skill_format_rules}

Document: {filename}
Main skill name: {main_skill_name}
Main skill description: {main_skill_description}

Sub-skills already generated (each is a separate .md file agents can import):
{sub_skills_list}

Instructions:
1. Begin with the YAML frontmatter:
   name: {main_skill_name}
   description: {main_skill_description}
2. Write a concise overview of what the full document covers.
3. Include a "## Sub-skills" section that lists every sub-skill with:
   - The filename to import (e.g. `kafka_producer_setup.md`)
   - One sentence on when to call it
   Example format:
   ### kafka_producer_setup
   File: `kafka_producer_setup.md`
   Load when: setting up a Kafka producer in Python.
4. Include a "## Quick reference" section with the most critical cross-cutting rules from the document.
5. Do NOT duplicate content already in the sub-skills; link/reference instead.
6. Keep it concise — this is an index and router, not a repeat of the sub-skills.

Document overview (first 4000 chars):
<document_excerpt>
{document_excerpt}
</document_excerpt>
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubSkillDescriptor:
    """Descrittore di un sub-skill identificato dall'LLM."""
    name: str
    description: str
    section_hint: str


@dataclass
class Config:
    """Contiene i parametri runtime necessari per contattare l'endpoint LLM e generare le skill."""
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
    """Parsa gli argomenti CLI e restituisce una configurazione validata."""
    parser = argparse.ArgumentParser(
        description=(
            "Converte un PDF o DOCX in una famiglia di skill Markdown "
            "usando un endpoint OpenAI-compatible."
        )
    )
    parser.add_argument("input_file", help="Percorso del file sorgente .pdf o .docx")
    parser.add_argument(
        "-o", "--output-dir",
        default="skills_output",
        help="Directory di output dove salvare i file .md generati (default: skills_output/)",
    )
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"), help="Base URL OpenAI-compatible")
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"), help="API key del provider")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"), help="Modello da usare")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Dimensione massima del testo inviato all'LLM per sub-skill")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP, help="Overlap tra chunk (usato solo se il documento è enorme)")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("-v", "--verbose", action="store_true", default=False)

    args = parser.parse_args(argv)
    if args.max_chars < 1000:
        raise ValueError("--max-chars deve essere almeno 1000")
    if not args.base_url or not args.api_key or not args.model:
        raise ValueError("base-url, api-key e model sono obbligatori (o via env)")

    return Config(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        model=args.model,
        input_path=Path(args.input_file),
        output_dir=Path(args.output_dir),
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

def read_input_file(input_path: Path) -> str:
    """Legge un file PDF o DOCX e ne estrae il testo grezzo."""
    if not input_path.exists():
        raise FileNotFoundError(f"File non trovato: {input_path}")

    suffix = input_path.suffix.lower()
    log.info("Lettura file: %s  (formato: %s)", input_path.name, suffix.lstrip(".").upper())

    if suffix == ".pdf":
        text = extract_text_from_pdf(input_path)
    elif suffix == ".docx":
        text = extract_text_from_docx(input_path)
    else:
        raise ValueError("Formato non supportato. Usa un file .pdf o .docx")

    cleaned = normalize_text(text)
    if not cleaned.strip():
        raise ValueError("Nessun testo estraibile dal documento")

    log.info("Testo estratto: %d caratteri", len(cleaned))
    return cleaned


def extract_text_from_pdf(input_path: Path) -> str:
    reader = PdfReader(str(input_path))
    total_pages = len(reader.pages)
    log.info("PDF: %d pagine trovate", total_pages)
    parts: List[str] = []
    for index, page in enumerate(reader.pages, start=1):
        log.debug("  Estrazione pagina %d/%d ...", index, total_pages)
        page_text = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
        parts.append(f"\n\n[PAGE {index}]\n{page_text}")
    log.info("Estrazione PDF completata")
    return "".join(parts)


def iter_block_items(parent):
    if isinstance(parent, _Document):
        parent_element = parent.element.body
    elif isinstance(parent, _Cell):
        parent_element = parent._tc
    else:
        raise ValueError("Tipo DOCX non supportato per l'iterazione dei blocchi")
    for child in parent_element.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def extract_text_from_docx(input_path: Path) -> str:
    log.info("Apertura documento DOCX ...")
    document = Document(str(input_path))
    blocks: List[str] = []
    n_paragraphs = 0
    n_tables = 0
    for block in iter_block_items(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                blocks.append(text)
                n_paragraphs += 1
        elif isinstance(block, Table):
            for row in block.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    blocks.append(f"[TABLE] {row_text}")
            n_tables += 1
    log.info("DOCX: %d paragrafi, %d tabelle estratti", n_paragraphs, n_tables)
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u00a0", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def truncate_for_llm(text: str, max_chars: int) -> str:
    """Tronca il testo a max_chars preservando gli ultimi caratteri come overlap implicito."""
    if len(text) <= max_chars:
        return text
    log.warning("Testo troncato da %d a %d caratteri per rispettare --max-chars", len(text), max_chars)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def call_chat_completion(
    config: Config,
    messages: List[dict],
    response_format: dict | None = None,
    label: str = "LLM",
) -> str:
    """Effettua una chiamata al endpoint chat completions compatibile OpenAI."""
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    url = f"{config.base_url}/chat/completions"
    log.debug("  → POST %s  [%s]", url, label)

    t_start = time.monotonic()
    response = requests.post(
        url,
        headers=build_headers(config.api_key),
        json=payload,
        timeout=config.timeout,
    )
    elapsed = time.monotonic() - t_start

    if response.status_code >= 400:
        raise RuntimeError(f"Errore HTTP {response.status_code}: {response.text}")

    data = response.json()
    log.debug("  ← risposta ricevuta in %.1fs  [%s]", elapsed, label)

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Risposta inattesa dal provider: {json.dumps(data)[:1200]}") from exc

    usage = data.get("usage")
    if usage:
        log.debug(
            "  token: prompt=%s  completion=%s  total=%s",
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            usage.get("total_tokens", "?"),
        )

    return content


# ---------------------------------------------------------------------------
# Phase A – identify sub-skills
# ---------------------------------------------------------------------------

def identify_sub_skills(config: Config, document_text: str) -> List[SubSkillDescriptor]:
    """Chiama l'LLM per identificare i sub-skill presenti nel documento."""
    log.info("--- Fase A: Identificazione sub-skill ---")
    truncated = truncate_for_llm(document_text, config.max_chars)

    prompt = IDENTIFY_SKILLS_PROMPT.format(
        filename=config.input_path.name,
        source_type=config.input_path.suffix.lower().lstrip("."),
        document_text=truncated,
    )

    content = call_chat_completion(
        config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        label="identify sub-skills",
    )

    # The model may return a top-level object with a list inside
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON non valido nella fase di identificazione: {content[:1000]}") from exc

    # Normalize: accept both a bare list and {"skills": [...]} or similar wrappers
    if isinstance(parsed, list):
        raw_list = parsed
    elif isinstance(parsed, dict):
        # find first key whose value is a list
        raw_list = next((v for v in parsed.values() if isinstance(v, list)), [])
    else:
        raw_list = []

    descriptors: List[SubSkillDescriptor] = []
    for item in raw_list:
        name = re.sub(r"[^a-z0-9_]", "_", (item.get("name") or "skill").lower()).strip("_")
        description = (item.get("description") or "").strip()
        section_hint = (item.get("section_hint") or "").strip()
        if name:
            descriptors.append(SubSkillDescriptor(name=name, description=description, section_hint=section_hint))

    if not descriptors:
        log.warning("Nessun sub-skill identificato; genero un unico sub-skill generico.")
        stem = re.sub(r"[^a-z0-9_]", "_", config.input_path.stem.lower())
        descriptors.append(SubSkillDescriptor(
            name=stem or "skill",
            description=f"Use when working with content from {config.input_path.name}.",
            section_hint="entire document",
        ))

    log.info("Sub-skill identificati: %d", len(descriptors))
    for d in descriptors:
        log.info("  • %s  — %s", d.name, d.section_hint)

    return descriptors


# ---------------------------------------------------------------------------
# Phase B – generate each sub-skill
# ---------------------------------------------------------------------------

def generate_sub_skill(config: Config, descriptor: SubSkillDescriptor, document_text: str) -> str:
    """Genera il contenuto Markdown di un singolo sub-skill."""
    log.info("  Generazione sub-skill: %s ...", descriptor.name)
    truncated = truncate_for_llm(document_text, config.max_chars)

    prompt = SUB_SKILL_PROMPT.format(
        skill_format_rules=SKILL_FORMAT_RULES,
        skill_name=descriptor.name,
        skill_description=descriptor.description,
        section_hint=descriptor.section_hint,
        filename=config.input_path.name,
        source_type=config.input_path.suffix.lower().lstrip("."),
        document_text=truncated,
    )

    t_start = time.monotonic()
    content = call_chat_completion(
        config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        label=f"sub-skill:{descriptor.name}",
    ).strip()
    elapsed = time.monotonic() - t_start

    log.info("  ✓ %s completato in %.1fs  (%d caratteri)", descriptor.name, elapsed, len(content))
    return content


# ---------------------------------------------------------------------------
# Phase C – generate main skill
# ---------------------------------------------------------------------------

def generate_main_skill(
    config: Config,
    document_text: str,
    descriptors: List[SubSkillDescriptor],
) -> str:
    """Genera la skill principale che referenzia tutti i sub-skill."""
    log.info("--- Fase C: Generazione skill principale ---")

    main_name = re.sub(r"[^a-z0-9_]", "_", config.input_path.stem.lower()).strip("_") or "main_skill"
    main_description = (
        f"Master skill for {config.input_path.name}. "
        "Load this to understand which sub-skill to use for a given task."
    )

    sub_skills_lines = []
    for d in descriptors:
        sub_skills_lines.append(
            f"- name: {d.name}\n"
            f"  file: {d.name}.md\n"
            f"  description: {d.description}\n"
            f"  section_hint: {d.section_hint}"
        )
    sub_skills_list = "\n".join(sub_skills_lines)

    prompt = MAIN_SKILL_PROMPT.format(
        skill_format_rules=SKILL_FORMAT_RULES,
        filename=config.input_path.name,
        main_skill_name=main_name,
        main_skill_description=main_description,
        sub_skills_list=sub_skills_list,
        document_excerpt=document_text[:4000],
    )

    t_start = time.monotonic()
    content = call_chat_completion(
        config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        label="main skill",
    ).strip()
    elapsed = time.monotonic() - t_start

    log.info("Skill principale generata in %.1fs  (%d caratteri)", elapsed, len(content))
    return content, main_name


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_skill(output_dir: Path, skill_name: str, content: str) -> Path:
    """Salva il contenuto di una skill nel file <output_dir>/<skill_name>.md."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{skill_name}.md"
    out_path.write_text(content, encoding="utf-8")
    log.info("  Salvato: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def generate_skills(config: Config) -> List[Path]:
    """Esegue l'intera pipeline multi-skill e restituisce i percorsi dei file generati."""
    t_pipeline_start = time.monotonic()
    log.info("=== Skill Generator (multi-skill) avviato ===")
    log.info("Sorgente  : %s", config.input_path)
    log.info("Output dir: %s", config.output_dir)
    log.info("Modello   : %s  @ %s", config.model, config.base_url)

    # --- Fase 1: estrazione testo ---
    log.info("--- Fase 1/4: Estrazione testo ---")
    document_text = read_input_file(config.input_path)

    # --- Fase 2: identificazione sub-skill ---
    log.info("--- Fase 2/4: Identificazione sub-skill ---")
    descriptors = identify_sub_skills(config, document_text)

    # --- Fase 3: generazione sub-skill ---
    log.info("--- Fase 3/4: Generazione sub-skill (%d) ---", len(descriptors))
    saved_paths: List[Path] = []
    for i, descriptor in enumerate(descriptors, start=1):
        log.info("[%d/%d] %s", i, len(descriptors), descriptor.name)
        content = generate_sub_skill(config, descriptor, document_text)
        path = save_skill(config.output_dir, descriptor.name, content)
        saved_paths.append(path)
        if i < len(descriptors):
            time.sleep(0.2)

    # --- Fase 4: generazione skill principale ---
    log.info("--- Fase 4/4: Generazione skill principale ---")
    main_content, main_name = generate_main_skill(config, document_text, descriptors)
    main_path = save_skill(config.output_dir, main_name, main_content)
    saved_paths.append(main_path)

    elapsed_total = time.monotonic() - t_pipeline_start
    log.info("=== Pipeline completata in %.1fs — %d file generati ===", elapsed_total, len(saved_paths))
    return saved_paths


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Punto d'ingresso CLI del programma."""
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
