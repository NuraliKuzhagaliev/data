"""Local installation is disabled after the acceptance test failed."""
import sys

if __name__ == "__main__":
    print("Локальный режим не принят: Qwen3.5-9B не прошла длинный тест. Используйте облачный API. Подробности: docs/night-v4/local-experiment.md")
    sys.exit(2)
