"""
Diagnose the language-model setup:  python -m jarvis.llm.check
(on the VM:  sudo docker compose exec jarvis python -m jarvis.llm.check)

For each configured provider: which models this key can use, which one JARVIS picks per tier,
and a one-word test call. Never prints keys.
"""

from jarvis.config import Settings
from jarvis.llm.providers import build_llms


def main() -> None:
    s = Settings()
    fast, smart = build_llms(s)
    print(f"mode={s.llm_mode}  groq_key={'set' if s.groq_api_key else 'missing'}  "
          f"gemini_key={'set' if s.gemini_api_key else 'missing'}  openrouter_key={'set' if s.openrouter_api_key else 'missing'}")
    for tier, chain in (("fast", fast), ("smart", smart)):
        for p in chain.providers:
            if getattr(p, "is_offline_engine", False):
                continue
            line = f"[{tier}] {p.family:<10}"
            try:
                models = p.list_models() if hasattr(p, "list_models") else []
                line += f" {len(models)} models visible to this key;"
                if hasattr(p, "resolve") and (p.model is None or p.model not in models):
                    p.resolve()
                out = p.complete("Reply with the single word OK.", max_tokens=5)
                line += f" using {p.model} -> test reply {out.strip()[:20]!r}  OK"
            except Exception as e:
                line += f" FAILED: {str(e)[:220]}"
            print(line)


if __name__ == "__main__":
    main()
