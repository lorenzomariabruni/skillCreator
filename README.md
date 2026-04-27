# PDF/DOCX Skill Generator

Tool CLI in Python che legge un file PDF o DOCX, lo divide in chunk e invia ogni chunk a un endpoint **OpenAI-compatible** per ottenere una skill Markdown pronta da importare in agenti come Roo Code, Claude Code o sistemi simili.

## Obiettivo

Il programma è pensato per trasformare documentazione, guide, manuali, prompt book o procedure in una skill riusabile e ottimizzata per agenti AI.

Caratteristiche principali:
- Estrazione testo da PDF con `pypdf`.
- Estrazione testo da DOCX con mantenimento dell'ordine tra paragrafi e tabelle.
- Chunking con overlap configurabile.
- Chiamate a endpoint compatibili con `POST /chat/completions`.
- Pipeline in due fasi: estrazione strutturata per chunk, poi fusione finale in una skill coerente.
- Preservazione **verbatim** di esempi di codice, config, JSON, YAML, SQL, comandi shell e snippet tecnici quando il modello li riporta dal sorgente.
- Documentazione delle funzioni in stile docstring simile a Javadoc, adattata a Python.

## Struttura progetto

```text
pdf-docx-skill-generator/
├── skill_generator.py
├── requirements.txt
└── README.md
```

## Requisiti

- Python 3.11+
- Un endpoint OpenAI-compatible
- Base URL, API key e modello

Installa le dipendenze:

```bash
pip install -r requirements.txt
```

## Endpoint supportato

Il programma invia richieste HTTP verso un endpoint compatibile con la Chat Completions API, normalmente esposto come `POST /chat/completions`.

La `base-url` deve puntare alla radice API compatibile, per esempio:

```text
https://host.example/v1
```

Da lì il programma chiamerà:

```text
https://host.example/v1/chat/completions
```

## Uso rapido

Con parametri espliciti:

```bash
python skill_generator.py manuale.pdf \\
  --base-url https://host.example/v1 \\
  --api-key sk-xxxxx \\
  --model gpt-4.1-mini \\
  --output skill-manuale.md
```

Con variabili ambiente:

```bash
export OPENAI_BASE_URL="https://host.example/v1"
export OPENAI_API_KEY="sk-xxxxx"
export OPENAI_MODEL="gpt-4.1-mini"

python skill_generator.py guida.docx --output output-skill.md
```

## Parametri CLI

| Parametro | Descrizione |
|---|---|
| `input_file` | File sorgente `.pdf` o `.docx`. |
| `-o`, `--output` | File Markdown di output. |
| `--base-url` | Base URL dell'API OpenAI-compatible. |
| `--api-key` | API key del provider. |
| `--model` | Nome del modello da usare. |
| `--max-chars` | Numero massimo di caratteri per chunk. |
| `--overlap` | Overlap tra chunk consecutivi. |
| `--temperature` | Temperatura del modello. |
| `--timeout` | Timeout HTTP in secondi. |
| `--max-output-tokens` | Numero massimo di token generati per chiamata. |

## Pipeline

1. Il file viene letto e convertito in testo.
2. Il testo viene normalizzato e suddiviso in chunk con overlap.
3. Ogni chunk viene inviato all'LLM con un prompt che estrae struttura utile per una skill.
4. I risultati dei chunk vengono aggregati e deduplicati.
5. Un secondo prompt genera la skill finale in Markdown.
6. Il file viene salvato su disco.

## Strategia di prompt

La qualità del risultato dipende in gran parte dal prompt. In questo progetto il prompt è stato ottimizzato per:

- trasformare contenuto descrittivo in istruzioni operative per un agente;
- preservare snippet di codice ed esempi tecnici **esattamente come nel sorgente**;
- rimuovere rumore tipico di PDF e OCR, come header, footer e numeri pagina;
- fondere chunk multipli in una skill unica e coerente;
- evitare invenzioni, endpoint non presenti o API non documentate nel documento sorgente.

### Fase 1 — estrazione per chunk

Ogni chunk viene convertito in JSON strutturato. Questo rende più robusta la fase di merge finale, perché i campi vengono deduplicati e ricombinati in modo controllato.

### Fase 2 — skill finale

Il prompt finale costruisce una skill con sezioni precise:

- `Purpose`
- `When to use`
- `Inputs expected`
- `Core instructions`
- `Constraints`
- `Workflow`
- `Output format`
- `Examples`
- `Source notes`

## Estrazione PDF e DOCX

L'estrazione PDF usa `pypdf`, che supporta `extract_text()` e una modalità `layout` per aderire meglio alla disposizione del testo nel file sorgente.

L'estrazione DOCX usa `python-docx`, dove il paragrafo è l'unità base del testo; per mantenere l'ordine corretto tra paragrafi e tabelle viene usata un'iterazione sui blocchi del documento a livello XML.

## Limiti noti

- PDF molto scannerizzati o con OCR scarso possono produrre testo rumoroso; il prompt cerca di mitigarlo ma non può ricostruire perfettamente contenuti illeggibili.
- Se il provider non supporta `response_format={"type": "json_object"}`, potrebbe essere necessario rimuovere quel parametro o adattarlo alla compatibilità del provider.
- Il mantenimento verbatim del codice dipende dalla qualità del testo estratto dal documento originale.
- Alcuni provider OpenAI-compatible implementano solo sottoinsiemi dell'API; conviene verificare il supporto reale del proprio backend.

## Miglioramenti possibili

- Supporto OCR per PDF scannerizzati.
- Riconoscimento più avanzato dei blocchi di codice in DOCX.
- Retry con backoff esponenziale su errori transienti.
- Output opzionale in YAML o formato skill-specifico per framework particolari.
- Supporto streaming e logging strutturato.

## Note per l'import in agenti

Il file generato è Markdown puro, quindi è facile da adattare a sistemi diversi:
- import diretto come skill/testo istruzionale;
- conversione in YAML frontmatter + Markdown;
- incorporazione in repository di prompt o cataloghi di skill.

## Licenza d'uso

Adattare secondo il proprio progetto.
