# Readback

Self-hosted reading comprehension and points tracking for families.

Readback imports your book list, generates short quizzes, and puts every
question behind a parent approval gate. Readers take approved quizzes and earn
points for passing. Your data stays in a local SQLite file.

Readback is not affiliated with Renaissance Learning or Accelerated Reader. It
generates original questions and never copies proprietary quiz content.

## Status

Early MVP, under active construction. See [SPEC.md](SPEC.md) for the design
contract and the current scope.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/readback init-db
.venv/bin/readback serve
```

Then open http://127.0.0.1:5057.

Configuration is environment based. See `SPEC.md` for the full list. Quiz
generation defaults to a local OpenAI-compatible endpoint at
`http://localhost:11434/v1`, so Ollama or LM Studio works without an API key.

## License

MIT. See [LICENSE](LICENSE).
