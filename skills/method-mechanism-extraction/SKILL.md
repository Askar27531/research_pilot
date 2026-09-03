---
name: method-mechanism-extraction
description: Extract a paper's method mechanism into evidence-grounded structured fields.
version: 1.0.0
---

# Method mechanism extraction

Use only the supplied verified passages. A `supported_fact` must cite one or more supplied
Evidence IDs. When a passage does not establish a requested field, return a concise `inference`
with no Evidence ID. Describe causal steps and assumptions; do not replace mechanism with a
paper abstract or marketing claim. Never infer novelty.
