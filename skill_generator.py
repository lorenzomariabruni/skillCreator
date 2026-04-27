#!/usr/bin/env python3
"""CLI per convertire PDF o DOCX in una skill per agenti tramite endpoint OpenAI-compatible."""

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
    """Configura e restituisce il logger principale dell'applicazione.

    Il logger scrive su stderr con timestamp, livello e messaggio.
    Se ``verbose`` è True il livello è DEBUG, altrimenti INFO.

    Args:
        verbose: Abilita il livello DEBUG per messaggi di dettaglio aggiuntivi.

    Returns:
        Logger configurato con il nome ``skill_generator``.
    """
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
# Prompt constants
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior AI agent skill author.
Your task is to transform source material extracted from PDF or DOCX files into a HIGH-QUALITY reusable skill file that can be imported into coding agents such as Roo Code, Claude Code style agents, or similar systems.

Core objective:
- Produce one self-contained skill document in Markdown.
- Preserve the source intent, procedures, rules, heuristics, glossary, and examples.
- Reorganize the content for maximum usefulness to another AI agent.
- Remove repetition, OCR noise, boilerplate page furniture, and irrelevant fragments.

Critical preservation rules:
- If the source contains code examples, commands, config blocks, schemas, prompts, regexes, JSON, YAML, XML, SQL, or pseudocode, preserve them EXACTLY when you include them.
- Do not rewrite code examples unless the source itself contains obvious OCR corruption; if corruption is suspected, explicitly mark the snippet as possibly corrupted instead of silently fixing it.
- Preserve parameter names, flags, environment variables, identifiers, paths, URLs, and API shapes exactly as found.
- Do not summarize code into prose when the code itself carries instructional value.

Output requirements:
Return ONLY valid Markdown for the skill.
The skill must follow this structure exactly:

# <clear skill title>

## Purpose
2-6 bullets describing what the skill helps an agent do.

## When to use
Bullets describing triggers, suitable tasks, and non-goals.

## Inputs expected
Bullets describing what information or files the agent should have before using the skill.

## Core instructions
Numbered steps with operational guidance.

## Constraints
Bullets describing hard rules, safety rails, formatting requirements, and failure conditions.

## Workflow
A concise ordered procedure the agent should follow.

## Output format
Describe the expected structure/style of the agent's final answer or artifact.

## Examples
Include source-grounded examples whenever available.
If there are exact code examples in the source, place them in fenced code blocks and preserve them verbatim.

## Source notes
Short bullets about ambiguities, missing context, or likely OCR issues.

Optimization rules:
- Prefer imperative instructions addressed to the agent.
- Convert descriptive material into operational rules.
- Make the skill specific, concrete, and execution-oriented.
- Merge duplicates.
- Keep the most authoritative formulation when duplicates conflict.
- Preserve domain terminology.
- If the source is fragmented across chunks, reconcile it into one coherent skill.
- If a later chunk refines an earlier rule, keep the refined rule.
- Do not invent features, APIs, commands, or examples not supported by the source.

Style rules:
- Clear, dense, agent-oriented writing.
- Minimal fluff.
- High information density.
- Markdown only.
- No prefatory commentary.
- No closing commentary.
"""

CHUNK_PROMPT_TEMPLATE = """You are extracting structured skill material from a source document.
This is chunk {chunk_index} of {total_chunks}.
Source filename: {filename}
Source type: {source_type}

Tasks:
1. Extract the operationally useful content from this chunk.
2. Keep exact code/config/examples verbatim when present.
3. Discard headers, footers, page numbers, repeated legal boilerplate, and obvious OCR garbage.
4. Rewrite prose into concise agent-facing instructions when possible.
5. If content appears partial because of chunk boundaries, note that briefly.

Return ONLY a JSON object with this schema:
{{
  "title_candidates": ["..."],
  "purpose": ["..."],
  "when_to_use": ["..."],
  "inputs_expected": ["..."],
  "core_instructions": ["..."],
  "constraints": ["..."],
  "workflow": ["..."],
  "output_format": ["..."],
  "examples": [{{"kind": "code|text", "content": "...", "verbatim": true|false}}],
  "source_notes": ["..."]
}}

Additional rules:
- For exact code/examples copied from the source, set verbatim=true.
- Keep examples complete when possible.
- Do not wrap the JSON in markdown fences.

Chunk content:
<chunk>
{chunk_text}
</chunk>
"""

FINAL_PROMPT_TEMPLATE = """You are merging extracted chunk analyses into one final skill.
Source filename: {filename}
Source type: {source_type}

Below is the normalized information extracted from all chunks.
Unify it into one final skill markdown.

Rules:
- Follow the system instructions exactly.
- Deduplicate aggressively.
- Preserve exact code/examples verbatim when they were marked verbatim.
- Prefer the most concrete and actionable wording.
- Resolve overlap into a single coherent document.
- Mention ambiguities only in 'Source notes'.
- Return only Markdown.

Aggregated chunk data:
{aggregated_json}
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChunkResult:
    """Rappresenta il risultato strutturato ottenuto dall'LLM per un singolo chunk."""

    title_candidates: List[str]
    purpose: List[str]
    when_to_use: List[str]
    inputs_expected: List[str]
    core_instructions: List[str]
    constraints: List[str]
    workflow: List[str]
    output_format: List[str]
    examples: List[dict]
    source_notes: List[str]


@dataclass
class Config:
    """Contiene i parametri runtime necessari per contattare l'endpoint LLM e generare la skill."""

    base_url: str
    api_key: str
    model: str
    input_path: Path
    output_path: Path
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
    """Parsa gli argomenti CLI e restituisce una configurazione validata.

    Args:
        argv: Sequenza di argomenti passata dal chiamante, tipicamente ``sys.argv[1:]``.

    Returns:
        Un'istanza ``Config`` contenente i parametri operativi del programma.

    Raises:
        SystemExit: Sollevata automaticamente da argparse in caso di uso non valido.
        ValueError: Se i parametri numerici non rispettano i vincoli minimi.
    """
    parser = argparse.ArgumentParser(
        description="Converte un PDF o DOCX in una skill Markdown usando un endpoint OpenAI-compatible."
    )
    parser.add_argument("input_file", help="Percorso del file sorgente .pdf o .docx")
    parser.add_argument("-o", "--output", default="generated_skill.md", help="File Markdown di output")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"), help="Base URL OpenAI-compatible")
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"), help="API key del provider")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"), help="Modello da usare")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Dimensione massima chunk")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP, help="Overlap tra chunk")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="Temperature LLM")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout HTTP in secondi")
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Massimo numero di token generati per chiamata",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Abilita log DEBUG (include dimensioni chunk, tempi per chiamata, ecc.)",
    )

    args = parser.parse_args(argv)
    if args.max_chars < 1000:
        raise ValueError("--max-chars deve essere almeno 1000")
    if args.overlap < 0 or args.overlap >= args.max_chars:
        raise ValueError("--overlap deve essere >= 0 e minore di --max-chars")
    if not args.base_url or not args.api_key or not args.model:
        raise ValueError("base-url, api-key e model sono obbligatori (o via env)")

    return Config(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        model=args.model,
        input_path=Path(args.input_file),
        output_path=Path(args.output),
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
    """Legge un file PDF o DOCX e ne estrae il testo grezzo in ordine sequenziale.

    Args:
        input_path: Percorso del file di input.

    Returns:
        Il testo concatenato estratto dal documento.

    Raises:
        FileNotFoundError: Se il file non esiste.
        ValueError: Se l'estensione non è supportata oppure il testo è vuoto.
    """
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
    """Estrae testo da un PDF usando pypdf.

    Args:
        input_path: Percorso del PDF.

    Returns:
        Una stringa contenente il testo estratto da tutte le pagine.
    """
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


def iter_block_items(parent: _Document | _Cell) -> Iterable[Paragraph | Table]:
    """Itera paragrafi e tabelle di un DOCX mantenendo l'ordine del documento.

    Args:
        parent: Documento Word principale o cella di tabella.

    Yields:
        Oggetti ``Paragraph`` o ``Table`` nell'ordine originale del file.

    Raises:
        ValueError: Se il tipo del contenitore non è gestito.
    """
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
    """Estrae testo da un DOCX preservando l'ordine tra paragrafi e tabelle.

    Args:
        input_path: Percorso del documento DOCX.

    Returns:
        Testo del documento, incluse righe tabellari serializzate in formato leggibile.
    """
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
    """Normalizza il testo estratto riducendo rumore tipografico comune.

    Args:
        text: Testo sorgente estratto dal file.

    Returns:
        Testo normalizzato con spazi e ritorni a capo più regolari.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u00a0", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    """Divide il testo in chunk sovrapposti per invio incrementale all'LLM.

    Args:
        text: Testo completo del documento.
        max_chars: Lunghezza massima di ciascun chunk in caratteri.
        overlap: Numero di caratteri condivisi tra chunk consecutivi.

    Returns:
        Lista di chunk ordinati.
    """
    if len(text) <= max_chars:
        log.info("Documento piccolo: 1 solo chunk (%d caratteri)", len(text))
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            split_at = text.rfind("\n\n", start, end)
            if split_at == -1 or split_at <= start + max_chars // 3:
                split_at = text.rfind("\n", start, end)
            if split_at != -1 and split_at > start:
                end = split_at
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(0, end - overlap)

    log.info(
        "Chunking: %d chunk generati  (max_chars=%d, overlap=%d)",
        len(chunks), max_chars, overlap,
    )
    for i, c in enumerate(chunks, start=1):
        log.debug("  Chunk %d: %d caratteri", i, len(c))

    return chunks


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_headers(api_key: str) -> dict:
    """Costruisce gli header HTTP per una API OpenAI-compatible.

    Args:
        api_key: Chiave API del provider.

    Returns:
        Dizionario di header HTTP pronto per la chiamata POST.
    """
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
    """Effettua una chiamata al endpoint chat completions compatibile OpenAI.

    Args:
        config: Configurazione runtime con URL, modello e timeout.
        messages: Messaggi chat da inviare al modello.
        response_format: Eventuale vincolo di formato risposta, se supportato.
        label: Etichetta descrittiva mostrata nel log per identificare la chiamata.

    Returns:
        Il contenuto testuale della prima choice restituita dal provider.

    Raises:
        RuntimeError: Se la risposta HTTP o il payload non sono validi.
    """
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

    # Log utilizzo token se il provider lo restituisce
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
# Chunk extraction
# ---------------------------------------------------------------------------

def extract_chunk_structure(config: Config, chunk: str, chunk_index: int, total_chunks: int) -> ChunkResult:
    """Invia un chunk all'LLM e ne ricava una struttura JSON normalizzata.

    Args:
        config: Configurazione runtime.
        chunk: Testo del chunk corrente.
        chunk_index: Indice 1-based del chunk.
        total_chunks: Numero totale di chunk.

    Returns:
        Un oggetto ``ChunkResult`` con il materiale estratto dal chunk.

    Raises:
        RuntimeError: Se l'LLM non restituisce JSON valido.
    """
    log.info(
        "[%d/%d] Elaborazione chunk (%d caratteri) ...",
        chunk_index, total_chunks, len(chunk),
    )

    prompt = CHUNK_PROMPT_TEMPLATE.format(
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        filename=config.input_path.name,
        source_type=config.input_path.suffix.lower().lstrip("."),
        chunk_text=chunk,
    )

    t_start = time.monotonic()
    content = call_chat_completion(
        config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        label=f"chunk {chunk_index}/{total_chunks}",
    )
    elapsed = time.monotonic() - t_start

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON non valido nel chunk {chunk_index}: {content[:1000]}") from exc

    n_examples = len(payload.get("examples") or [])
    log.info(
        "[%d/%d] ✓ chunk completato in %.1fs  (esempi trovati: %d)",
        chunk_index, total_chunks, elapsed, n_examples,
    )

    return ChunkResult(
        title_candidates=payload.get("title_candidates", []) or [],
        purpose=payload.get("purpose", []) or [],
        when_to_use=payload.get("when_to_use", []) or [],
        inputs_expected=payload.get("inputs_expected", []) or [],
        core_instructions=payload.get("core_instructions", []) or [],
        constraints=payload.get("constraints", []) or [],
        workflow=payload.get("workflow", []) or [],
        output_format=payload.get("output_format", []) or [],
        examples=payload.get("examples", []) or [],
        source_notes=payload.get("source_notes", []) or [],
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    """Rimuove duplicati preservando il primo ordinamento utile.

    Args:
        items: Collezione iterabile di stringhe.

    Returns:
        Lista senza duplicati, normalizzata e senza elementi vuoti.
    """
    seen = set()
    result: List[str] = []
    for item in items:
        normalized = re.sub(r"\s+", " ", item or "").strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def aggregate_chunk_results(results: Sequence[ChunkResult]) -> dict:
    """Aggrega i risultati dei chunk in una struttura unica da rifinire nel prompt finale.

    Args:
        results: Risultati strutturati provenienti dai singoli chunk.

    Returns:
        Dizionario serializzabile in JSON con materiale consolidato.
    """
    log.info("Aggregazione risultati da %d chunk ...", len(results))

    aggregated_examples: List[dict] = []
    seen_examples = set()

    for result in results:
        for example in result.examples:
            content = (example or {}).get("content", "").strip()
            verbatim = bool((example or {}).get("verbatim", False))
            kind = (example or {}).get("kind", "text").strip() or "text"
            if not content:
                continue
            key = (kind, verbatim, content)
            if key in seen_examples:
                continue
            seen_examples.add(key)
            aggregated_examples.append({"kind": kind, "content": content, "verbatim": verbatim})

    aggregated = {
        "title_candidates": dedupe_preserve_order(
            item for result in results for item in result.title_candidates
        ),
        "purpose": dedupe_preserve_order(item for result in results for item in result.purpose),
        "when_to_use": dedupe_preserve_order(item for result in results for item in result.when_to_use),
        "inputs_expected": dedupe_preserve_order(
            item for result in results for item in result.inputs_expected
        ),
        "core_instructions": dedupe_preserve_order(
            item for result in results for item in result.core_instructions
        ),
        "constraints": dedupe_preserve_order(item for result in results for item in result.constraints),
        "workflow": dedupe_preserve_order(item for result in results for item in result.workflow),
        "output_format": dedupe_preserve_order(
            item for result in results for item in result.output_format
        ),
        "examples": aggregated_examples,
        "source_notes": dedupe_preserve_order(item for result in results for item in result.source_notes),
    }

    log.debug(
        "  Aggregazione: %d istruzioni, %d vincoli, %d esempi",
        len(aggregated["core_instructions"]),
        len(aggregated["constraints"]),
        len(aggregated["examples"]),
    )
    return aggregated


# ---------------------------------------------------------------------------
# Final skill rendering
# ---------------------------------------------------------------------------

def render_final_skill(config: Config, aggregated: dict) -> str:
    """Richiede all'LLM la skill finale in Markdown a partire dai dati aggregati.

    Args:
        config: Configurazione runtime.
        aggregated: Informazioni consolidate estratte da tutti i chunk.

    Returns:
        Skill finale in formato Markdown.
    """
    log.info("Generazione skill finale (chiamata LLM di merge) ...")

    prompt = FINAL_PROMPT_TEMPLATE.format(
        filename=config.input_path.name,
        source_type=config.input_path.suffix.lower().lstrip("."),
        aggregated_json=json.dumps(aggregated, ensure_ascii=False, indent=2),
    )

    t_start = time.monotonic()
    result = call_chat_completion(
        config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format=None,
        label="final merge",
    ).strip()
    elapsed = time.monotonic() - t_start

    log.info("Skill finale generata in %.1fs  (%d caratteri)", elapsed, len(result))
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_text(output_path: Path, content: str) -> None:
    """Salva testo UTF-8 su disco creando le directory mancanti.

    Args:
        output_path: Percorso destinazione del file.
        content: Contenuto testuale da serializzare.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    log.info("File salvato: %s", output_path)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def generate_skill(config: Config) -> Path:
    """Esegue l'intera pipeline: estrazione, chunking, chiamate LLM e salvataggio finale.

    Args:
        config: Configurazione completa del processo.

    Returns:
        Percorso del file skill generato.
    """
    t_pipeline_start = time.monotonic()
    log.info("=== Skill Generator avviato ===")
    log.info("Sorgente : %s", config.input_path)
    log.info("Output   : %s", config.output_path)
    log.info("Modello  : %s  @ %s", config.model, config.base_url)
    log.info("Parametri: max_chars=%d  overlap=%d  temperature=%s", config.max_chars, config.overlap, config.temperature)

    # --- Fase 1: estrazione testo ---
    log.info("--- Fase 1/4: Estrazione testo ---")
    source_text = read_input_file(config.input_path)

    # --- Fase 2: chunking ---
    log.info("--- Fase 2/4: Chunking ---")
    chunks = chunk_text(source_text, max_chars=config.max_chars, overlap=config.overlap)
    log.info("Totale chunk da elaborare: %d", len(chunks))

    # --- Fase 3: chiamate LLM per chunk ---
    log.info("--- Fase 3/4: Analisi chunk via LLM ---")
    results: List[ChunkResult] = []
    for index, chunk in enumerate(chunks, start=1):
        result = extract_chunk_structure(config, chunk, index, len(chunks))
        results.append(result)
        if index < len(chunks):
            time.sleep(0.2)

    # --- Fase 4: merge finale ---
    log.info("--- Fase 4/4: Merge e generazione skill finale ---")
    aggregated = aggregate_chunk_results(results)
    final_skill = render_final_skill(config, aggregated)
    save_text(config.output_path, final_skill)

    elapsed_total = time.monotonic() - t_pipeline_start
    log.info("=== Pipeline completata in %.1fs ===", elapsed_total)
    return config.output_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Punto d'ingresso CLI del programma.

    Args:
        argv: Argomenti opzionali; se assenti usa ``sys.argv[1:]``.

    Returns:
        Codice di uscita POSIX, ``0`` per successo e ``1`` per errore.
    """
    try:
        config = parse_args(argv if argv is not None else sys.argv[1:])
        # Inizializza il logger globale con il livello scelto dall'utente
        global log
        log = setup_logging(verbose=config.verbose)
        output_path = generate_skill(config)
        print(f"\n✓ Skill generata in: {output_path}")
        return 0
    except Exception as exc:
        logging.getLogger("skill_generator").error("Errore fatale: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
